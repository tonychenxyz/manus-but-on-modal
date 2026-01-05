"""Home Orchestrator - The brain of Agent Home.

Uses the Claude Agent SDK (ClaudeSDKClient) for session-based conversations
to process user messages, generate plans, and coordinate worker sandbox execution.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
)

from agent_home.orchestrator.mcp_tools import (
    create_orchestrator_mcp_server,
    ORCHESTRATOR_TOOL_NAMES,
)
from shared.config import get_settings
from shared.models import (
    ConversationStatus,
    Event,
    EventType,
    JobSpec,
    Run,
    RunState,
    WorkerJob,
    WorkerJobStatus,
)

if TYPE_CHECKING:
    from agent_home.persistence import EventStore, StateManager

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Home Orchestrator, the central coordinator for an async multi-conversation agent system.

Your responsibilities:
1. Understand user requests and create execution plans
2. Coordinate work by spawning worker sandboxes for code operations
3. Aggregate results from workers and present them to users
4. Maintain memory and knowledge across conversations

Key constraints:
- You do NOT have direct access to repositories or code
- All code work must be done by spawning worker sandboxes
- You orchestrate and coordinate, workers execute
- Plans require user approval before execution

When creating a plan:
1. Break down the task into clear steps
2. Identify what worker jobs are needed
3. Specify the repos and goals for each worker
4. Consider dependencies between workers

When a user asks you to do something that requires code changes:
1. First understand what they want
2. Create a plan with specific worker jobs
3. Wait for approval before executing

You have access to persistent memory to store facts, preferences, and project information.
Use self-prompts to remind yourself of things to follow up on."""


class HomeOrchestrator:
    """Orchestrates conversation processing and worker coordination.

    Uses the Claude Agent SDK's ClaudeSDKClient for session-based conversations,
    coordinating with worker sandboxes for actual code execution.
    """

    def __init__(
        self,
        state_manager: "StateManager",
        event_store: "EventStore",
        memory_path: Path,
        worker_spawner: Any = None,
    ):
        """Initialize the orchestrator.

        Args:
            state_manager: State manager for persistence
            event_store: Event store for event logging
            memory_path: Path to the memory directory
            worker_spawner: Callback to spawn worker sandboxes
        """
        self.state_manager = state_manager
        self.event_store = event_store
        self.memory_path = memory_path
        self.worker_spawner = worker_spawner

        settings = get_settings()
        self.model = settings.orchestrator_model

        # Track pending workers during planning
        self._pending_workers: list[dict[str, Any]] = []

        # Create MCP server with orchestrator tools
        self.mcp_server = create_orchestrator_mcp_server(
            memory_path=memory_path,
            state_manager=state_manager,
            event_store=event_store,
            pending_workers_callback=self._on_pending_worker,
        )

        # Track active sessions and tasks for cancellation
        self._active_sessions: dict[str, ClaudeSDKClient] = {}
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancelled_runs: set[str] = set()

    def _on_pending_worker(self, worker: dict[str, Any]) -> None:
        """Callback when a worker is queued during planning."""
        self._pending_workers.append(worker)

    def _get_agent_options(
        self,
        session_id: str | None = None,
        permission_mode: str = "acceptEdits",
    ) -> ClaudeAgentOptions:
        """Get ClaudeAgentOptions for the orchestrator.

        Args:
            session_id: Optional session ID to resume
            permission_mode: Permission mode for the agent

        Returns:
            Configured ClaudeAgentOptions
        """
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=SYSTEM_PROMPT,
            permission_mode=permission_mode,
            max_turns=50,
            mcp_servers={"orchestrator": self.mcp_server},
            allowed_tools=ORCHESTRATOR_TOOL_NAMES,
        )

        if session_id:
            options.resume = session_id

        return options

    async def start_planning(self, run: Run) -> None:
        """Start the planning phase for a run.

        Generates a plan based on the user's message without executing anything.

        Args:
            run: The run to plan
        """
        task = asyncio.create_task(self._planning_loop(run))
        self._active_tasks[run.run_id] = task

        try:
            await task
        except asyncio.CancelledError:
            logger.info(f"Planning cancelled for run {run.run_id}")
        finally:
            self._active_tasks.pop(run.run_id, None)

    async def _planning_loop(self, run: Run) -> None:
        """Internal planning loop using ClaudeSDKClient.

        Args:
            run: The run to plan
        """
        logger.info(f"Starting planning for run {run.run_id}")

        # Clear pending workers
        self._pending_workers.clear()

        try:
            # Build conversation context
            history = await self._build_conversation_history(run.conversation_id)

            # Create planning prompt
            planning_prompt = f"""The user has sent a new message:

<user_message>
{run.user_message}
</user_message>

Previous conversation context:
{history}

Please analyze this request and create a plan. Your plan should:
1. Describe what needs to be done
2. List any worker jobs that need to be spawned (use spawn_worker tool)
3. Explain the expected outcomes

If the task is simple and doesn't require code changes, you can respond directly.
If it requires code work, use spawn_worker to define the jobs needed.

After creating the plan, the user will need to approve it before execution begins."""

            # Create client with options
            options = self._get_agent_options(permission_mode="acceptEdits")

            plan_text = ""

            async with ClaudeSDKClient(options=options) as client:
                # Store session for potential cancellation
                self._active_sessions[run.run_id] = client

                # Send the planning query
                await client.query(planning_prompt)

                # Process responses
                async for message in client.receive_response():
                    if run.run_id in self._cancelled_runs:
                        await client.interrupt()
                        raise asyncio.CancelledError()

                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                plan_text += block.text

                                # Stream thinking to events
                                await self.event_store.append(
                                    Event(
                                        cursor=0,
                                        conversation_id=run.conversation_id,
                                        type=EventType.ASSISTANT_THINKING,
                                        payload={"content": block.text},
                                        run_id=run.run_id,
                                    )
                                )

                            elif isinstance(block, ToolUseBlock):
                                logger.debug(
                                    f"Tool call: {block.name} with {block.input}"
                                )

                    elif isinstance(message, ResultMessage):
                        logger.info(
                            f"Planning completed: {message.num_turns} turns, "
                            f"{message.duration_ms}ms"
                        )

                # Remove session reference
                self._active_sessions.pop(run.run_id, None)

            # Collect pending workers from MCP server
            pending_workers = self.mcp_server.get_pending_workers()  # type: ignore
            job_specs = [w["spec"] for w in pending_workers]

            # Also include any from our callback
            for w in self._pending_workers:
                if w["spec"] not in job_specs:
                    job_specs.append(w["spec"])

            # Update run with plan
            await self.state_manager.update_run(
                run.run_id,
                plan=plan_text,
                job_specs=job_specs,
            )

            # Transition to waiting approval
            await self.state_manager.transition_run_state(
                run.run_id,
                RunState.WAITING_APPROVAL,
            )

            # Emit plan ready event
            await self.event_store.append(
                Event(
                    cursor=0,
                    conversation_id=run.conversation_id,
                    type=EventType.RUN_PLAN_READY,
                    payload={
                        "run_id": run.run_id,
                        "plan_markdown": plan_text,
                        "jobs": [
                            {
                                "job_id": s.job_id,
                                "repo": s.repo.url,
                                "ref": s.repo.ref,
                                "goal": s.goal,
                            }
                            for s in job_specs
                        ],
                    },
                    run_id=run.run_id,
                )
            )

            # Update conversation status
            await self.state_manager.update_conversation(
                run.conversation_id,
                status=ConversationStatus.WAITING_APPROVAL,
            )

            logger.info(
                f"Planning complete for run {run.run_id}, "
                f"{len(job_specs)} worker jobs planned"
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Planning failed for run {run.run_id}: {e}")
            await self._fail_run(run, str(e))

    async def execute_run(self, run: Run) -> None:
        """Execute an approved run.

        Spawns worker sandboxes and coordinates their execution.

        Args:
            run: The run to execute (must be in RUNNING state)
        """
        task = asyncio.create_task(self._execution_loop(run))
        self._active_tasks[run.run_id] = task

        try:
            await task
        except asyncio.CancelledError:
            logger.info(f"Execution cancelled for run {run.run_id}")
        finally:
            self._active_tasks.pop(run.run_id, None)
            self._cancelled_runs.discard(run.run_id)

    async def _execution_loop(self, run: Run) -> None:
        """Internal execution loop.

        Args:
            run: The run to execute
        """
        logger.info(f"Starting execution for run {run.run_id}")

        try:
            # Emit run started event
            await self.event_store.append(
                Event(
                    cursor=0,
                    conversation_id=run.conversation_id,
                    type=EventType.RUN_STARTED,
                    payload={"run_id": run.run_id},
                    run_id=run.run_id,
                )
            )

            # Spawn worker jobs
            worker_job_ids: list[str] = []
            for spec in run.job_specs:
                if run.run_id in self._cancelled_runs:
                    raise asyncio.CancelledError()

                job = await self._spawn_worker_job(run, spec)
                worker_job_ids.append(job.job_id)

            # Update run with worker job IDs
            await self.state_manager.update_run(
                run.run_id,
                worker_jobs=worker_job_ids,
            )

            # Wait for all workers to complete
            results = await self._wait_for_workers(run, worker_job_ids)

            if run.run_id in self._cancelled_runs:
                raise asyncio.CancelledError()

            # Generate summary response using Agent SDK
            summary = await self._generate_summary(run, results)

            # Emit assistant message
            await self.event_store.append(
                Event(
                    cursor=0,
                    conversation_id=run.conversation_id,
                    type=EventType.MESSAGE_ASSISTANT,
                    payload={"content": summary},
                    run_id=run.run_id,
                )
            )

            # Check if any workers failed
            failed = [r for r in results if r.status == WorkerJobStatus.FAILED]
            if failed:
                await self.state_manager.transition_run_state(
                    run.run_id,
                    RunState.FAILED,
                    error=f"{len(failed)} worker(s) failed",
                )
                await self.event_store.append(
                    Event(
                        cursor=0,
                        conversation_id=run.conversation_id,
                        type=EventType.RUN_FAILED,
                        payload={
                            "run_id": run.run_id,
                            "failed_jobs": [r.job_id for r in failed],
                        },
                        run_id=run.run_id,
                    )
                )
            else:
                await self.state_manager.transition_run_state(
                    run.run_id,
                    RunState.COMPLETED,
                )
                await self.event_store.append(
                    Event(
                        cursor=0,
                        conversation_id=run.conversation_id,
                        type=EventType.RUN_COMPLETED,
                        payload={"run_id": run.run_id},
                        run_id=run.run_id,
                    )
                )

            # Update conversation status
            await self.state_manager.update_conversation(
                run.conversation_id,
                status=ConversationStatus.IDLE,
                active_run_id=None,
            )

            logger.info(f"Execution complete for run {run.run_id}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Execution failed for run {run.run_id}: {e}")
            await self._fail_run(run, str(e))

    async def _spawn_worker_job(self, run: Run, spec: JobSpec) -> WorkerJob:
        """Spawn a worker job.

        Args:
            run: The parent run
            spec: The job specification

        Returns:
            The created worker job
        """
        job = WorkerJob(
            job_id=spec.job_id,
            run_id=run.run_id,
            spec=spec,
            status=WorkerJobStatus.QUEUED,
        )

        await self.state_manager.create_worker_job(job)

        # Emit job queued event
        await self.event_store.append(
            Event(
                cursor=0,
                conversation_id=run.conversation_id,
                type=EventType.JOB_QUEUED,
                payload={
                    "job_id": job.job_id,
                    "repo": spec.repo.url,
                    "goal": spec.goal,
                },
                run_id=run.run_id,
                job_id=job.job_id,
            )
        )

        # Spawn the actual worker (via Modal)
        if self.worker_spawner:
            sandbox_id = await self.worker_spawner(spec)
            await self.state_manager.update_worker_job(
                job.job_id,
                sandbox_id=sandbox_id,
                status=WorkerJobStatus.RUNNING,
                started_at=datetime.utcnow(),
            )

            await self.event_store.append(
                Event(
                    cursor=0,
                    conversation_id=run.conversation_id,
                    type=EventType.JOB_STARTED,
                    payload={"job_id": job.job_id, "sandbox_id": sandbox_id},
                    run_id=run.run_id,
                    job_id=job.job_id,
                )
            )

        return job

    async def _wait_for_workers(
        self, run: Run, job_ids: list[str]
    ) -> list[WorkerJob]:
        """Wait for all worker jobs to complete.

        Args:
            run: The parent run
            job_ids: IDs of jobs to wait for

        Returns:
            List of completed worker jobs
        """
        completed: list[WorkerJob] = []

        while len(completed) < len(job_ids):
            if run.run_id in self._cancelled_runs:
                raise asyncio.CancelledError()

            await asyncio.sleep(2)  # Poll interval

            for job_id in job_ids:
                if any(j.job_id == job_id for j in completed):
                    continue

                job = await self.state_manager.get_worker_job(job_id)
                if not job:
                    continue

                if job.status in (
                    WorkerJobStatus.SUCCEEDED,
                    WorkerJobStatus.FAILED,
                    WorkerJobStatus.CANCELLED,
                ):
                    completed.append(job)

                    # Emit completion event
                    event_type = (
                        EventType.JOB_COMPLETED
                        if job.status == WorkerJobStatus.SUCCEEDED
                        else EventType.JOB_FAILED
                    )
                    await self.event_store.append(
                        Event(
                            cursor=0,
                            conversation_id=run.conversation_id,
                            type=event_type,
                            payload={
                                "job_id": job.job_id,
                                "status": job.status.value,
                                "result": job.result.model_dump() if job.result else None,
                            },
                            run_id=run.run_id,
                            job_id=job.job_id,
                        )
                    )

        return completed

    async def _generate_summary(
        self, run: Run, worker_jobs: list[WorkerJob]
    ) -> str:
        """Generate a summary of the run results using Agent SDK.

        Args:
            run: The completed run
            worker_jobs: Completed worker jobs

        Returns:
            Summary text for the user
        """
        # Build summary from worker results
        results_text = []
        for job in worker_jobs:
            status = "✓" if job.status == WorkerJobStatus.SUCCEEDED else "✗"
            result_summary = ""
            if job.result:
                if job.result.pr_url:
                    result_summary = f"PR: {job.result.pr_url}"
                elif job.result.summary:
                    result_summary = job.result.summary
                elif job.result.error:
                    result_summary = f"Error: {job.result.error}"

            results_text.append(
                f"{status} **{job.spec.goal}**\n   {result_summary}"
            )

        if not results_text:
            return "Task completed. No worker jobs were needed."

        # Use Agent SDK to generate a natural summary
        prompt = f"""Please provide a brief, friendly summary of the following task results for the user.

Original request: {run.user_message}

Plan: {run.plan}

Worker Results:
{chr(10).join(results_text)}

Keep the summary concise and highlight the key outcomes (like PR links)."""

        try:
            options = ClaudeAgentOptions(
                model=self.model,
                system_prompt="You are a helpful assistant summarizing task results. Be concise and friendly.",
                max_turns=1,
            )

            summary = ""
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                summary += block.text

            return summary if summary else self._fallback_summary(results_text)

        except Exception as e:
            logger.warning(f"Failed to generate summary with Agent SDK: {e}")
            return self._fallback_summary(results_text)

    def _fallback_summary(self, results_text: list[str]) -> str:
        """Generate a simple fallback summary."""
        return f"## Results\n\n" + "\n".join(results_text)

    async def cancel_run(self, run: Run) -> None:
        """Cancel a run and its workers.

        Args:
            run: The run to cancel
        """
        logger.info(f"Cancelling run {run.run_id}")

        self._cancelled_runs.add(run.run_id)

        # Interrupt active session if any
        session = self._active_sessions.get(run.run_id)
        if session:
            try:
                await session.interrupt()
            except Exception as e:
                logger.warning(f"Failed to interrupt session: {e}")

        # Cancel the active task
        task = self._active_tasks.get(run.run_id)
        if task:
            task.cancel()

        # Cancel worker jobs
        jobs = await self.state_manager.get_jobs_for_run(run.run_id)
        for job in jobs:
            if job.status in (WorkerJobStatus.QUEUED, WorkerJobStatus.RUNNING):
                await self.state_manager.update_worker_job(
                    job.job_id,
                    status=WorkerJobStatus.CANCELLED,
                    completed_at=datetime.utcnow(),
                )

    async def _fail_run(self, run: Run, error: str) -> None:
        """Mark a run as failed.

        Args:
            run: The run that failed
            error: Error message
        """
        await self.state_manager.transition_run_state(
            run.run_id,
            RunState.FAILED,
            error=error,
        )

        await self.event_store.append(
            Event(
                cursor=0,
                conversation_id=run.conversation_id,
                type=EventType.RUN_FAILED,
                payload={"run_id": run.run_id, "error": error},
                run_id=run.run_id,
            )
        )

        await self.state_manager.update_conversation(
            run.conversation_id,
            status=ConversationStatus.ERROR,
            active_run_id=None,
        )

    async def _build_conversation_history(
        self, conversation_id: str
    ) -> str:
        """Build conversation history string for context.

        Args:
            conversation_id: The conversation ID

        Returns:
            Formatted history string
        """
        events = await self.event_store.get_events(
            conversation_id=conversation_id,
            after=0,
            limit=50,
            event_types=[EventType.MESSAGE_USER, EventType.MESSAGE_ASSISTANT],
        )

        history_parts = []
        for event in events:
            role = "User" if event.type == EventType.MESSAGE_USER else "Assistant"
            content = event.payload.get("content", "")[:500]  # Truncate long messages
            history_parts.append(f"{role}: {content}")

        return "\n\n".join(history_parts) if history_parts else "No previous messages."

    async def resume_active_runs(self) -> None:
        """Resume any runs that were active when Agent Home restarted.

        Called during startup to recover from restarts.
        """
        active_runs = await self.state_manager.get_active_runs()

        for run in active_runs:
            logger.info(f"Resuming run {run.run_id} in state {run.state}")

            if run.state == RunState.PLANNING:
                # Restart planning
                asyncio.create_task(self.start_planning(run))
            elif run.state == RunState.RUNNING:
                # Resume execution (workers may still be running)
                asyncio.create_task(self.execute_run(run))
