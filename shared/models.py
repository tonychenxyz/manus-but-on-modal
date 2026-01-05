"""Core data models for Agent Home Orchestrator.

Defines the primary entities: Conversation, Run, WorkerJob, Event, and their schemas.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ============================================================================
# Enums
# ============================================================================


class ConversationStatus(str, Enum):
    """Status of a conversation."""

    IDLE = "idle"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    ERROR = "error"


class RunState(str, Enum):
    """State machine for a Run.

    Valid transitions:
        PLANNING -> WAITING_APPROVAL (plan generated)
        WAITING_APPROVAL -> RUNNING (approved)
        WAITING_APPROVAL -> DENIED (denied by user)
        RUNNING -> COMPLETED (all jobs finished successfully)
        RUNNING -> FAILED (one or more jobs failed)
        RUNNING -> CANCELLED (user cancelled)
        QUEUED -> PLANNING (when previous run completes)
    """

    QUEUED = "queued"  # Waiting for previous run to complete
    PLANNING = "planning"  # Agent is generating plan
    WAITING_APPROVAL = "waiting_approval"  # Plan ready, waiting for user approval
    RUNNING = "running"  # Approved and executing
    COMPLETED = "completed"  # Successfully finished
    FAILED = "failed"  # One or more jobs failed
    DENIED = "denied"  # User denied the plan
    CANCELLED = "cancelled"  # User cancelled during execution


class WorkerJobStatus(str, Enum):
    """Status of a worker job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerJobKind(str, Enum):
    """Kind of work a worker job performs."""

    IMPLEMENT = "implement"
    TEST = "test"
    INVESTIGATE = "investigate"
    REFACTOR = "refactor"
    BUILD = "build"
    CUSTOM = "custom"


class EventType(str, Enum):
    """Types of events in the event stream."""

    # Message events
    MESSAGE_USER = "message.user"
    MESSAGE_ASSISTANT = "message.assistant"
    MESSAGE_SYSTEM = "message.system"

    # Run lifecycle events
    RUN_CREATED = "run.created"
    RUN_PLAN_READY = "run.plan_ready"
    RUN_APPROVED = "run.approved"
    RUN_DENIED = "run.denied"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"

    # Worker job events
    JOB_QUEUED = "job.queued"
    JOB_STARTED = "job.started"
    JOB_PROGRESS = "job.progress"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"

    # Streaming events
    ASSISTANT_TOKEN = "assistant.token"
    ASSISTANT_THINKING = "assistant.thinking"

    # Memory/curation events
    MEMORY_UPDATED = "memory.updated"
    SELF_PROMPT_CREATED = "self_prompt.created"


# ============================================================================
# Core Models
# ============================================================================


class RepoRef(BaseModel):
    """Reference to a git repository."""

    url: str = Field(..., description="Git repository URL")
    ref: str = Field(default="main", description="Git ref (branch, tag, or commit)")


class RuntimeProfile(BaseModel):
    """Runtime configuration for worker sandbox."""

    language: str = Field(default="python", description="Primary language")
    bootstrap: list[str] = Field(
        default_factory=list, description="Commands to run after clone"
    )
    env: dict[str, str] = Field(
        default_factory=dict, description="Environment variables"
    )


class JobConstraints(BaseModel):
    """Constraints for worker job execution."""

    no_secrets_in_output: bool = Field(default=True)
    produce_pr: bool = Field(default=False)
    timeout_seconds: int = Field(default=3600, description="Max execution time")


class JobSpec(BaseModel):
    """Specification for a worker job.

    Sent from Agent Home to Worker Sandbox to define what work to perform.
    """

    job_id: str = Field(..., description="Unique job identifier")
    repo: RepoRef = Field(..., description="Repository to work on")
    goal: str = Field(..., description="What the worker should accomplish")
    kind: WorkerJobKind = Field(default=WorkerJobKind.IMPLEMENT)
    constraints: JobConstraints = Field(default_factory=JobConstraints)
    runtime_profile: RuntimeProfile = Field(default_factory=RuntimeProfile)
    context: dict[str, Any] = Field(
        default_factory=dict, description="Additional context from orchestrator"
    )


class WorkerResult(BaseModel):
    """Result produced by a worker job."""

    job_id: str
    status: WorkerJobStatus
    pr_url: str | None = None
    commit_hash: str | None = None
    branch_name: str | None = None
    summary: str = Field(default="", description="Human-readable summary of work done")
    logs_path: str | None = Field(None, description="Path to detailed logs")
    artifacts: dict[str, Any] = Field(
        default_factory=dict, description="Additional artifacts"
    )
    error: str | None = Field(None, description="Error message if failed")
    duration_seconds: float | None = None


class WorkerJob(BaseModel):
    """A worker job tracked by Agent Home."""

    job_id: str
    run_id: str
    spec: JobSpec
    status: WorkerJobStatus = WorkerJobStatus.QUEUED
    result: WorkerResult | None = None
    sandbox_id: str | None = Field(None, description="Modal sandbox ID")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Run(BaseModel):
    """A run represents a unit of work that requires approval.

    Every user message creates a run that must be approved before execution.
    """

    run_id: str
    conversation_id: str
    state: RunState = RunState.PLANNING
    user_message: str = Field(..., description="The user message that triggered this run")
    plan: str | None = Field(None, description="Generated plan (markdown)")
    job_specs: list[JobSpec] = Field(default_factory=list)
    worker_jobs: list[str] = Field(
        default_factory=list, description="Job IDs of spawned workers"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    approved_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class Conversation(BaseModel):
    """A conversation containing multiple runs."""

    conversation_id: str
    title: str = Field(default="New Conversation")
    status: ConversationStatus = ConversationStatus.IDLE
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    active_run_id: str | None = None
    run_queue: list[str] = Field(
        default_factory=list, description="Queued run IDs awaiting processing"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class Event(BaseModel):
    """An event in the append-only event stream.

    Events are the primary mechanism for:
    - Recording all state changes
    - Enabling catch-up after reconnection
    - Providing audit trail
    """

    cursor: int = Field(..., description="Monotonically increasing event ID")
    ts: datetime = Field(default_factory=datetime.utcnow)
    conversation_id: str
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    job_id: str | None = None


# ============================================================================
# API Request/Response Models
# ============================================================================


class CreateConversationRequest(BaseModel):
    """Request to create a new conversation."""

    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateConversationResponse(BaseModel):
    """Response after creating a conversation."""

    conversation_id: str
    conversation: Conversation


class SendMessageRequest(BaseModel):
    """Request to send a message (creates a new run)."""

    content: str = Field(..., min_length=1)


class SendMessageResponse(BaseModel):
    """Response after sending a message."""

    run_id: str
    run: Run


class ApproveRunRequest(BaseModel):
    """Request to approve a run."""

    pass  # No additional fields needed


class ApproveRunResponse(BaseModel):
    """Response after approving a run."""

    run_id: str
    state: RunState


class DenyRunRequest(BaseModel):
    """Request to deny a run."""

    reason: str | None = None


class DenyRunResponse(BaseModel):
    """Response after denying a run."""

    run_id: str
    state: RunState


class CancelRunRequest(BaseModel):
    """Request to cancel a running run."""

    pass


class CancelRunResponse(BaseModel):
    """Response after cancelling a run."""

    run_id: str
    state: RunState


class EventsResponse(BaseModel):
    """Response containing events for catch-up."""

    events: list[Event]
    next_cursor: int | None = None
    has_more: bool = False


class ConversationListResponse(BaseModel):
    """Response containing list of conversations."""

    conversations: list[Conversation]
    total: int
