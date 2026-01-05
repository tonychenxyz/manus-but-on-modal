"""State manager for conversations, runs, and worker jobs.

Provides CRUD operations and state machine management for the core entities.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from shared.models import (
    Conversation,
    ConversationStatus,
    JobSpec,
    Run,
    RunState,
    WorkerJob,
    WorkerJobStatus,
    WorkerResult,
)

logger = logging.getLogger(__name__)


# Valid state transitions for runs
RUN_STATE_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.QUEUED: {RunState.PLANNING, RunState.CANCELLED},
    RunState.PLANNING: {RunState.WAITING_APPROVAL, RunState.FAILED},
    RunState.WAITING_APPROVAL: {RunState.RUNNING, RunState.DENIED, RunState.CANCELLED},
    RunState.RUNNING: {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED},
    RunState.COMPLETED: set(),
    RunState.FAILED: set(),
    RunState.DENIED: set(),
    RunState.CANCELLED: set(),
}


class StateManager:
    """Manages state for conversations, runs, and worker jobs.

    Uses SQLite for durable storage with efficient queries.
    """

    def __init__(self, db_path: str | Path):
        """Initialize the state manager.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize the database and create tables."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row

        # Enable WAL mode
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

        # Create conversations table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                active_run_id TEXT,
                run_queue TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)

        # Create runs table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                state TEXT NOT NULL,
                user_message TEXT NOT NULL,
                plan TEXT,
                job_specs TEXT NOT NULL DEFAULT '[]',
                worker_jobs TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                approved_at TEXT,
                completed_at TEXT,
                error TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
            )
        """)

        # Create worker_jobs table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS worker_jobs (
                job_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                spec TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                sandbox_id TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            )
        """)

        # Create indexes
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_runs_conversation
            ON runs(conversation_id, created_at DESC)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_runs_state
            ON runs(state)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_run
            ON worker_jobs(run_id)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_status
            ON worker_jobs(status)
        """)

        # Create lease table for leader election
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS leader_lease (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        await self._db.commit()
        logger.info(f"State manager initialized at {self.db_path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    # =========================================================================
    # Conversation Operations
    # =========================================================================

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        """Create a new conversation.

        Args:
            conversation: The conversation to create

        Returns:
            The created conversation
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        await self._db.execute(
            """
            INSERT INTO conversations
            (conversation_id, title, status, created_at, updated_at,
             active_run_id, run_queue, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation.conversation_id,
                conversation.title,
                conversation.status.value,
                conversation.created_at.isoformat(),
                conversation.updated_at.isoformat(),
                conversation.active_run_id,
                json.dumps(conversation.run_queue),
                json.dumps(conversation.metadata),
            ),
        )
        await self._db.commit()

        return conversation

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Get a conversation by ID.

        Args:
            conversation_id: The conversation ID

        Returns:
            The conversation, or None if not found
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        return self._row_to_conversation(row)

    async def list_conversations(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """List conversations ordered by most recent activity.

        Args:
            limit: Maximum number to return
            offset: Number to skip

        Returns:
            List of conversations
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            """
            SELECT * FROM conversations
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()

        return [self._row_to_conversation(row) for row in rows]

    async def update_conversation(
        self,
        conversation_id: str,
        **updates: Any,
    ) -> Conversation | None:
        """Update a conversation.

        Args:
            conversation_id: The conversation to update
            **updates: Fields to update

        Returns:
            The updated conversation, or None if not found
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        # Always update updated_at
        updates["updated_at"] = datetime.utcnow()

        # Build update query
        set_clauses = []
        params = []
        for key, value in updates.items():
            if key == "status" and isinstance(value, ConversationStatus):
                value = value.value
            elif key in ("run_queue", "metadata"):
                value = json.dumps(value)
            elif key == "updated_at":
                value = value.isoformat()
            set_clauses.append(f"{key} = ?")
            params.append(value)

        params.append(conversation_id)

        await self._db.execute(
            f"UPDATE conversations SET {', '.join(set_clauses)} WHERE conversation_id = ?",
            params,
        )
        await self._db.commit()

        return await self.get_conversation(conversation_id)

    async def count_conversations(self) -> int:
        """Count total conversations.

        Returns:
            Total number of conversations
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            "SELECT COUNT(*) as count FROM conversations"
        ) as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    # =========================================================================
    # Run Operations
    # =========================================================================

    async def create_run(self, run: Run) -> Run:
        """Create a new run.

        Args:
            run: The run to create

        Returns:
            The created run
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        await self._db.execute(
            """
            INSERT INTO runs
            (run_id, conversation_id, state, user_message, plan, job_specs,
             worker_jobs, created_at, approved_at, completed_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.conversation_id,
                run.state.value,
                run.user_message,
                run.plan,
                json.dumps([s.model_dump() for s in run.job_specs]),
                json.dumps(run.worker_jobs),
                run.created_at.isoformat(),
                run.approved_at.isoformat() if run.approved_at else None,
                run.completed_at.isoformat() if run.completed_at else None,
                run.error,
            ),
        )
        await self._db.commit()

        return run

    async def get_run(self, run_id: str) -> Run | None:
        """Get a run by ID.

        Args:
            run_id: The run ID

        Returns:
            The run, or None if not found
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        return self._row_to_run(row)

    async def get_runs_for_conversation(
        self,
        conversation_id: str,
        limit: int = 50,
    ) -> list[Run]:
        """Get runs for a conversation.

        Args:
            conversation_id: The conversation ID
            limit: Maximum number to return

        Returns:
            List of runs ordered by creation time (newest first)
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            """
            SELECT * FROM runs
            WHERE conversation_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()

        return [self._row_to_run(row) for row in rows]

    async def transition_run_state(
        self,
        run_id: str,
        new_state: RunState,
        **updates: Any,
    ) -> Run | None:
        """Transition a run to a new state.

        Validates the transition is allowed.

        Args:
            run_id: The run to transition
            new_state: The target state
            **updates: Additional fields to update

        Returns:
            The updated run, or None if not found or transition invalid

        Raises:
            ValueError: If the state transition is not allowed
        """
        run = await self.get_run(run_id)
        if not run:
            return None

        # Validate transition
        allowed = RUN_STATE_TRANSITIONS.get(run.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Invalid state transition: {run.state} -> {new_state}. "
                f"Allowed: {allowed}"
            )

        # Update state and any additional fields
        updates["state"] = new_state.value

        # Set timestamps based on state
        now = datetime.utcnow()
        if new_state == RunState.RUNNING:
            updates["approved_at"] = now.isoformat()
        elif new_state in (
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.DENIED,
        ):
            updates["completed_at"] = now.isoformat()

        # Build and execute update
        set_clauses = []
        params = []
        for key, value in updates.items():
            if key == "job_specs":
                value = json.dumps([s.model_dump() for s in value])
            elif key == "worker_jobs":
                value = json.dumps(value)
            set_clauses.append(f"{key} = ?")
            params.append(value)

        params.append(run_id)

        if not self._db:
            raise RuntimeError("State manager not initialized")

        await self._db.execute(
            f"UPDATE runs SET {', '.join(set_clauses)} WHERE run_id = ?",
            params,
        )
        await self._db.commit()

        return await self.get_run(run_id)

    async def update_run(self, run_id: str, **updates: Any) -> Run | None:
        """Update a run without state transition validation.

        Use this for updates that don't change state.

        Args:
            run_id: The run to update
            **updates: Fields to update

        Returns:
            The updated run, or None if not found
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        # Build update query
        set_clauses = []
        params = []
        for key, value in updates.items():
            if key == "state" and isinstance(value, RunState):
                value = value.value
            elif key == "job_specs":
                value = json.dumps([s.model_dump() for s in value])
            elif key == "worker_jobs":
                value = json.dumps(value)
            set_clauses.append(f"{key} = ?")
            params.append(value)

        params.append(run_id)

        await self._db.execute(
            f"UPDATE runs SET {', '.join(set_clauses)} WHERE run_id = ?",
            params,
        )
        await self._db.commit()

        return await self.get_run(run_id)

    async def get_active_runs(self) -> list[Run]:
        """Get all runs that are currently active (PLANNING or RUNNING).

        Returns:
            List of active runs
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            """
            SELECT * FROM runs
            WHERE state IN (?, ?)
            ORDER BY created_at ASC
            """,
            (RunState.PLANNING.value, RunState.RUNNING.value),
        ) as cursor:
            rows = await cursor.fetchall()

        return [self._row_to_run(row) for row in rows]

    # =========================================================================
    # Worker Job Operations
    # =========================================================================

    async def create_worker_job(self, job: WorkerJob) -> WorkerJob:
        """Create a new worker job.

        Args:
            job: The job to create

        Returns:
            The created job
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        await self._db.execute(
            """
            INSERT INTO worker_jobs
            (job_id, run_id, spec, status, result, sandbox_id,
             created_at, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.run_id,
                job.spec.model_dump_json(),
                job.status.value,
                job.result.model_dump_json() if job.result else None,
                job.sandbox_id,
                job.created_at.isoformat(),
                job.started_at.isoformat() if job.started_at else None,
                job.completed_at.isoformat() if job.completed_at else None,
            ),
        )
        await self._db.commit()

        return job

    async def get_worker_job(self, job_id: str) -> WorkerJob | None:
        """Get a worker job by ID.

        Args:
            job_id: The job ID

        Returns:
            The job, or None if not found
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            "SELECT * FROM worker_jobs WHERE job_id = ?",
            (job_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        return self._row_to_worker_job(row)

    async def get_jobs_for_run(self, run_id: str) -> list[WorkerJob]:
        """Get all jobs for a run.

        Args:
            run_id: The run ID

        Returns:
            List of jobs
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            """
            SELECT * FROM worker_jobs
            WHERE run_id = ?
            ORDER BY created_at ASC
            """,
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        return [self._row_to_worker_job(row) for row in rows]

    async def update_worker_job(
        self,
        job_id: str,
        **updates: Any,
    ) -> WorkerJob | None:
        """Update a worker job.

        Args:
            job_id: The job to update
            **updates: Fields to update

        Returns:
            The updated job, or None if not found
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        # Build update query
        set_clauses = []
        params = []
        for key, value in updates.items():
            if key == "status" and isinstance(value, WorkerJobStatus):
                value = value.value
            elif key == "spec" and isinstance(value, JobSpec):
                value = value.model_dump_json()
            elif key == "result" and isinstance(value, WorkerResult):
                value = value.model_dump_json()
            elif key in ("started_at", "completed_at") and isinstance(value, datetime):
                value = value.isoformat()
            set_clauses.append(f"{key} = ?")
            params.append(value)

        params.append(job_id)

        await self._db.execute(
            f"UPDATE worker_jobs SET {', '.join(set_clauses)} WHERE job_id = ?",
            params,
        )
        await self._db.commit()

        return await self.get_worker_job(job_id)

    async def count_active_workers(self) -> int:
        """Count currently active worker jobs.

        Returns:
            Number of jobs in QUEUED or RUNNING state
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._db.execute(
            """
            SELECT COUNT(*) as count FROM worker_jobs
            WHERE status IN (?, ?)
            """,
            (WorkerJobStatus.QUEUED.value, WorkerJobStatus.RUNNING.value),
        ) as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0

    # =========================================================================
    # Leader Election
    # =========================================================================

    async def try_acquire_lease(
        self,
        holder_id: str,
        lease_duration_seconds: int = 60,
    ) -> bool:
        """Try to acquire or renew the leader lease.

        Args:
            holder_id: Unique identifier for this Agent Home instance
            lease_duration_seconds: How long the lease is valid

        Returns:
            True if lease acquired/renewed, False if another holder has it
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        now = datetime.utcnow()
        expires_at = datetime.utcnow()
        expires_at = now.replace(
            second=now.second + lease_duration_seconds
        )

        async with self._lock:
            # Check current lease
            async with self._db.execute(
                "SELECT * FROM leader_lease WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                current_holder = row["holder_id"]
                current_expires = datetime.fromisoformat(row["expires_at"])

                # If we hold it or it's expired, we can take it
                if current_holder == holder_id or current_expires < now:
                    await self._db.execute(
                        """
                        UPDATE leader_lease
                        SET holder_id = ?, acquired_at = ?, expires_at = ?
                        WHERE id = 1
                        """,
                        (holder_id, now.isoformat(), expires_at.isoformat()),
                    )
                    await self._db.commit()
                    return True
                else:
                    # Someone else holds a valid lease
                    return False
            else:
                # No lease exists, create one
                await self._db.execute(
                    """
                    INSERT INTO leader_lease (id, holder_id, acquired_at, expires_at)
                    VALUES (1, ?, ?, ?)
                    """,
                    (holder_id, now.isoformat(), expires_at.isoformat()),
                )
                await self._db.commit()
                return True

    async def release_lease(self, holder_id: str) -> bool:
        """Release the leader lease.

        Args:
            holder_id: The holder releasing the lease

        Returns:
            True if released, False if not the holder
        """
        if not self._db:
            raise RuntimeError("State manager not initialized")

        async with self._lock:
            result = await self._db.execute(
                "DELETE FROM leader_lease WHERE id = 1 AND holder_id = ?",
                (holder_id,),
            )
            await self._db.commit()
            return result.rowcount > 0

    # =========================================================================
    # Helpers
    # =========================================================================

    def _row_to_conversation(self, row: aiosqlite.Row) -> Conversation:
        """Convert a database row to a Conversation."""
        return Conversation(
            conversation_id=row["conversation_id"],
            title=row["title"],
            status=ConversationStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            active_run_id=row["active_run_id"],
            run_queue=json.loads(row["run_queue"]),
            metadata=json.loads(row["metadata"]),
        )

    def _row_to_run(self, row: aiosqlite.Row) -> Run:
        """Convert a database row to a Run."""
        job_specs_data = json.loads(row["job_specs"])
        return Run(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            state=RunState(row["state"]),
            user_message=row["user_message"],
            plan=row["plan"],
            job_specs=[JobSpec(**s) for s in job_specs_data],
            worker_jobs=json.loads(row["worker_jobs"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            approved_at=(
                datetime.fromisoformat(row["approved_at"])
                if row["approved_at"]
                else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
            error=row["error"],
        )

    def _row_to_worker_job(self, row: aiosqlite.Row) -> WorkerJob:
        """Convert a database row to a WorkerJob."""
        return WorkerJob(
            job_id=row["job_id"],
            run_id=row["run_id"],
            spec=JobSpec.model_validate_json(row["spec"]),
            status=WorkerJobStatus(row["status"]),
            result=(
                WorkerResult.model_validate_json(row["result"])
                if row["result"]
                else None
            ),
            sandbox_id=row["sandbox_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"])
                if row["started_at"]
                else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
        )
