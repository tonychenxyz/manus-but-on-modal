"""Home Orchestrator - The brain of Agent Home.

Uses the Anthropic API to process user messages, generate plans, and coordinate
worker sandbox execution.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anthropic

from agent_home.orchestrator.tools import ORCHESTRATOR_TOOLS, OrchestratorToolHandler
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

    Uses the Anthropic API for planning and response generation, coordinating
    with worker sandboxes for actual code execution.
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
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.orchestrator_model

        self.tool_handler = OrchestratorToolHandler(
            memory_path=memory_path,
            state_manager=state_manager,
            event_store=event_store,
            worker_spawner=worker_spawner,
        )

        # Track active runs for cancellation
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancelled_runs: set[str] = set()

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
        """Internal planning loop.

        Args:
            run: The run to plan
        """
        logger.info(f"Starting planning for run {run.run_id}")

        try:
            # Build conversation history
            messages = await self._build_conversation_history(run.conversation_id)

            # Add planning instruction
            planning_prompt = f"""The user has sent a new message:

<user_message>
{run.user_message}
</user_message>

Please analyze this request and create a plan. Your plan should:
1. Describe what needs to be done
2. List any worker jobs that need to be spawned (use spawn_worker tool)
3. Explain the expected outcomes

If the task is simple and doesn't require code changes, you can respond directly.
If it requires code work, use spawn_worker to define the jobs needed.

After creating the plan, the user will need to approve it before execution begins."""

            messages.append({"role": "user", "content": planning_prompt})

            # Run the planning conversation with tool use
            plan_text = ""
            all_tool_calls: list[dict[str, Any]] = []

            while True:
                if run.run_id in self._cancelled_runs:
                    raise asyncio.CancelledError()

                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=ORCHESTRATOR_TOOLS,  # type: ignore
                    messages=messages,  # type: ignore
                )

                # Process response blocks
                assistant_content: list[dict[str, Any]] = []
                tool_results: list[dict[str, Any]] = []

                for block in response.content:
                    if block.type == "text":
                        plan_text += block.text
                        assistant_content.append({
                            "type": "text",
                            "text": block.text,
                        })

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

                    elif block.type == "tool_use":
                        all_tool_calls.append({
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                        assistant_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })

                        # Execute the tool
                        result = await self.tool_handler.handle_tool(
                            block.name,
                            block.input,  # type: ignore
                            run.run_id,
                        )

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                # Add assistant response to messages
                messages.append({"role": "assistant", "content": assistant_content})

                # If there were tool calls, add results and continue
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                else:
                    # No more tool calls, planning complete
                    break

                if response.stop_reason == "end_turn":
                    break

            # Collect pending workers from tool calls
            pending_workers = self.tool_handler.get_pending_workers()
            job_specs = [w["spec"] for w in pending_workers]

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

            # Generate summary response
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
        from shared.models import WorkerResult

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
        """Generate a summary of the run results.

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

        # Use Claude to generate a natural summary
        messages = [
            {
                "role": "user",
                "content": f"""Please provide a brief, friendly summary of the following task results for the user.

Original request: {run.user_message}

Plan: {run.plan}

Worker Results:
{chr(10).join(results_text)}

Keep the summary concise and highlight the key outcomes (like PR links).""",
            }
        ]

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system="You are a helpful assistant summarizing task results. Be concise and friendly.",
                messages=messages,  # type: ignore
            )

            for block in response.content:
                if block.type == "text":
                    return block.text

        except Exception as e:
            logger.warning(f"Failed to generate summary with Claude: {e}")

        # Fallback to simple summary
        return f"## Results\n\n" + "\n".join(results_text)

    async def cancel_run(self, run: Run) -> None:
        """Cancel a run and its workers.

        Args:
            run: The run to cancel
        """
        logger.info(f"Cancelling run {run.run_id}")

        self._cancelled_runs.add(run.run_id)

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

                # TODO: Actually cancel the Modal sandbox
                # if job.sandbox_id and self.worker_canceller:
                #     await self.worker_canceller(job.sandbox_id)

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
    ) -> list[dict[str, Any]]:
        """Build conversation history for Claude.

        Args:
            conversation_id: The conversation ID

        Returns:
            List of messages in Claude format
        """
        events = await self.event_store.get_events(
            conversation_id=conversation_id,
            after=0,
            limit=100,
            event_types=[EventType.MESSAGE_USER, EventType.MESSAGE_ASSISTANT],
        )

        messages: list[dict[str, Any]] = []
        for event in events:
            if event.type == EventType.MESSAGE_USER:
                messages.append({
                    "role": "user",
                    "content": event.payload.get("content", ""),
                })
            elif event.type == EventType.MESSAGE_ASSISTANT:
                messages.append({
                    "role": "assistant",
                    "content": event.payload.get("content", ""),
                })

        return messages

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
