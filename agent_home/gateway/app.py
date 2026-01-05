"""FastAPI application for Agent Home Gateway.

Provides HTTP and WebSocket endpoints for the webapp to interact with Agent Home.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncGenerator

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware

from agent_home.gateway.auth import (
    TokenPayload,
    get_current_user,
    verify_websocket_token,
)
from shared.config import get_settings
from shared.models import (
    ApproveRunRequest,
    ApproveRunResponse,
    CancelRunRequest,
    CancelRunResponse,
    Conversation,
    ConversationListResponse,
    ConversationStatus,
    CreateConversationRequest,
    CreateConversationResponse,
    DenyRunRequest,
    DenyRunResponse,
    Event,
    EventsResponse,
    EventType,
    Run,
    RunState,
    SendMessageRequest,
    SendMessageResponse,
)

if TYPE_CHECKING:
    from agent_home.orchestrator import HomeOrchestrator
    from agent_home.persistence import EventStore, StateManager

logger = logging.getLogger(__name__)


# Global references set during app creation
_event_store: EventStore | None = None
_state_manager: StateManager | None = None
_orchestrator: HomeOrchestrator | None = None


def get_event_store() -> "EventStore":
    """Get the event store instance."""
    if _event_store is None:
        raise RuntimeError("Event store not initialized")
    return _event_store


def get_state_manager() -> "StateManager":
    """Get the state manager instance."""
    if _state_manager is None:
        raise RuntimeError("State manager not initialized")
    return _state_manager


def get_orchestrator() -> "HomeOrchestrator":
    """Get the orchestrator instance."""
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialized")
    return _orchestrator


def create_app(
    event_store: "EventStore",
    state_manager: "StateManager",
    orchestrator: "HomeOrchestrator",
) -> FastAPI:
    """Create the FastAPI application.

    Args:
        event_store: The event store for logging events
        state_manager: The state manager for entity CRUD
        orchestrator: The orchestrator for run processing

    Returns:
        Configured FastAPI application
    """
    global _event_store, _state_manager, _orchestrator
    _event_store = event_store
    _state_manager = state_manager
    _orchestrator = orchestrator

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Application lifespan handler."""
        logger.info("Agent Home Gateway starting up")
        yield
        logger.info("Agent Home Gateway shutting down")

    app = FastAPI(
        title="Agent Home Gateway",
        description="Gateway API for Agent Home Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Add CORS middleware
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure appropriately in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    _register_conversation_routes(app)
    _register_run_routes(app)
    _register_event_routes(app)
    _register_websocket_routes(app)
    _register_health_routes(app)

    return app


def _register_health_routes(app: FastAPI) -> None:
    """Register health check routes."""

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "healthy"}

    @app.get("/v1/status")
    async def get_status(
        user: TokenPayload = Depends(get_current_user),
    ) -> dict[str, Any]:
        """Get Agent Home status."""
        sm = get_state_manager()
        active_runs = await sm.get_active_runs()
        active_workers = await sm.count_active_workers()

        return {
            "status": "running",
            "active_runs": len(active_runs),
            "active_workers": active_workers,
            "user": user.email,
        }


def _register_conversation_routes(app: FastAPI) -> None:
    """Register conversation-related routes."""

    @app.post("/v1/conversations", response_model=CreateConversationResponse)
    async def create_conversation(
        request: CreateConversationRequest,
        user: TokenPayload = Depends(get_current_user),
    ) -> CreateConversationResponse:
        """Create a new conversation."""
        sm = get_state_manager()

        conversation = Conversation(
            conversation_id=f"conv_{uuid.uuid4().hex[:12]}",
            title=request.title or "New Conversation",
            status=ConversationStatus.IDLE,
            metadata=request.metadata,
        )

        await sm.create_conversation(conversation)

        logger.info(f"Created conversation {conversation.conversation_id}")

        return CreateConversationResponse(
            conversation_id=conversation.conversation_id,
            conversation=conversation,
        )

    @app.get("/v1/conversations", response_model=ConversationListResponse)
    async def list_conversations(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        user: TokenPayload = Depends(get_current_user),
    ) -> ConversationListResponse:
        """List all conversations."""
        sm = get_state_manager()

        conversations = await sm.list_conversations(limit=limit, offset=offset)
        total = await sm.count_conversations()

        return ConversationListResponse(
            conversations=conversations,
            total=total,
        )

    @app.get("/v1/conversations/{conversation_id}", response_model=Conversation)
    async def get_conversation(
        conversation_id: str,
        user: TokenPayload = Depends(get_current_user),
    ) -> Conversation:
        """Get a specific conversation."""
        sm = get_state_manager()

        conversation = await sm.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Conversation {conversation_id} not found",
            )

        return conversation

    @app.post(
        "/v1/conversations/{conversation_id}/messages",
        response_model=SendMessageResponse,
    )
    async def send_message(
        conversation_id: str,
        request: SendMessageRequest,
        user: TokenPayload = Depends(get_current_user),
    ) -> SendMessageResponse:
        """Send a message to a conversation, creating a new run."""
        sm = get_state_manager()
        es = get_event_store()
        orch = get_orchestrator()

        # Verify conversation exists
        conversation = await sm.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Conversation {conversation_id} not found",
            )

        # Create run
        run = Run(
            run_id=f"run_{uuid.uuid4().hex[:12]}",
            conversation_id=conversation_id,
            state=RunState.PLANNING,
            user_message=request.content,
        )

        await sm.create_run(run)

        # Log user message event
        await es.append(
            Event(
                cursor=0,  # Will be assigned
                conversation_id=conversation_id,
                type=EventType.MESSAGE_USER,
                payload={"content": request.content},
                run_id=run.run_id,
            )
        )

        # Log run created event
        await es.append(
            Event(
                cursor=0,
                conversation_id=conversation_id,
                type=EventType.RUN_CREATED,
                payload={"run_id": run.run_id, "user_message": request.content},
                run_id=run.run_id,
            )
        )

        # Update conversation status
        await sm.update_conversation(
            conversation_id,
            status=ConversationStatus.RUNNING,
            active_run_id=run.run_id,
        )

        # Start planning in background
        asyncio.create_task(orch.start_planning(run))

        logger.info(f"Created run {run.run_id} for message in {conversation_id}")

        return SendMessageResponse(run_id=run.run_id, run=run)


def _register_run_routes(app: FastAPI) -> None:
    """Register run-related routes."""

    @app.get("/v1/runs/{run_id}", response_model=Run)
    async def get_run(
        run_id: str,
        user: TokenPayload = Depends(get_current_user),
    ) -> Run:
        """Get a specific run."""
        sm = get_state_manager()

        run = await sm.get_run(run_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found",
            )

        return run

    @app.post("/v1/runs/{run_id}/approve", response_model=ApproveRunResponse)
    async def approve_run(
        run_id: str,
        request: ApproveRunRequest,
        user: TokenPayload = Depends(get_current_user),
    ) -> ApproveRunResponse:
        """Approve a run that is waiting for approval."""
        sm = get_state_manager()
        es = get_event_store()
        orch = get_orchestrator()

        run = await sm.get_run(run_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found",
            )

        if run.state != RunState.WAITING_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Run is in state {run.state}, expected WAITING_APPROVAL",
            )

        # Transition to RUNNING
        updated_run = await sm.transition_run_state(run_id, RunState.RUNNING)

        if not updated_run:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update run state",
            )

        # Log approval event
        await es.append(
            Event(
                cursor=0,
                conversation_id=run.conversation_id,
                type=EventType.RUN_APPROVED,
                payload={"run_id": run_id, "approved_by": user.email},
                run_id=run_id,
            )
        )

        # Start execution
        asyncio.create_task(orch.execute_run(updated_run))

        logger.info(f"Run {run_id} approved by {user.email}")

        return ApproveRunResponse(run_id=run_id, state=RunState.RUNNING)

    @app.post("/v1/runs/{run_id}/deny", response_model=DenyRunResponse)
    async def deny_run(
        run_id: str,
        request: DenyRunRequest,
        user: TokenPayload = Depends(get_current_user),
    ) -> DenyRunResponse:
        """Deny a run that is waiting for approval."""
        sm = get_state_manager()
        es = get_event_store()

        run = await sm.get_run(run_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found",
            )

        if run.state != RunState.WAITING_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Run is in state {run.state}, expected WAITING_APPROVAL",
            )

        # Transition to DENIED
        await sm.transition_run_state(
            run_id,
            RunState.DENIED,
            error=request.reason or "Denied by user",
        )

        # Log denial event
        await es.append(
            Event(
                cursor=0,
                conversation_id=run.conversation_id,
                type=EventType.RUN_DENIED,
                payload={
                    "run_id": run_id,
                    "denied_by": user.email,
                    "reason": request.reason,
                },
                run_id=run_id,
            )
        )

        # Update conversation status
        await sm.update_conversation(
            run.conversation_id,
            status=ConversationStatus.IDLE,
            active_run_id=None,
        )

        logger.info(f"Run {run_id} denied by {user.email}")

        return DenyRunResponse(run_id=run_id, state=RunState.DENIED)

    @app.post("/v1/runs/{run_id}/cancel", response_model=CancelRunResponse)
    async def cancel_run(
        run_id: str,
        request: CancelRunRequest,
        user: TokenPayload = Depends(get_current_user),
    ) -> CancelRunResponse:
        """Cancel a running run."""
        sm = get_state_manager()
        es = get_event_store()
        orch = get_orchestrator()

        run = await sm.get_run(run_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found",
            )

        if run.state not in (RunState.PLANNING, RunState.WAITING_APPROVAL, RunState.RUNNING):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Run in state {run.state} cannot be cancelled",
            )

        # Cancel via orchestrator (which will handle worker cleanup)
        await orch.cancel_run(run)

        # Transition to CANCELLED
        await sm.transition_run_state(run_id, RunState.CANCELLED)

        # Log cancellation event
        await es.append(
            Event(
                cursor=0,
                conversation_id=run.conversation_id,
                type=EventType.RUN_CANCELLED,
                payload={"run_id": run_id, "cancelled_by": user.email},
                run_id=run_id,
            )
        )

        # Update conversation status
        await sm.update_conversation(
            run.conversation_id,
            status=ConversationStatus.IDLE,
            active_run_id=None,
        )

        logger.info(f"Run {run_id} cancelled by {user.email}")

        return CancelRunResponse(run_id=run_id, state=RunState.CANCELLED)


def _register_event_routes(app: FastAPI) -> None:
    """Register event-related routes."""

    @app.get(
        "/v1/conversations/{conversation_id}/events",
        response_model=EventsResponse,
    )
    async def get_events(
        conversation_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        user: TokenPayload = Depends(get_current_user),
    ) -> EventsResponse:
        """Get events for a conversation (for catch-up)."""
        sm = get_state_manager()
        es = get_event_store()

        # Verify conversation exists
        conversation = await sm.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Conversation {conversation_id} not found",
            )

        events = await es.get_events(
            conversation_id=conversation_id,
            after=after,
            limit=limit + 1,  # Fetch one extra to check for more
        )

        has_more = len(events) > limit
        if has_more:
            events = events[:limit]

        next_cursor = events[-1].cursor if events else None

        return EventsResponse(
            events=events,
            next_cursor=next_cursor,
            has_more=has_more,
        )


def _register_websocket_routes(app: FastAPI) -> None:
    """Register WebSocket routes."""

    @app.websocket("/v1/stream")
    async def stream_events(
        websocket: WebSocket,
        conversation_id: str = Query(...),
        after: int = Query(default=0),
        token: str = Query(...),
    ) -> None:
        """WebSocket endpoint for streaming events."""
        # Verify token
        try:
            user = verify_websocket_token(token)
        except ValueError as e:
            await websocket.close(code=4001, reason=str(e))
            return

        sm = get_state_manager()
        es = get_event_store()

        # Verify conversation exists
        conversation = await sm.get_conversation(conversation_id)
        if not conversation:
            await websocket.close(code=4004, reason="Conversation not found")
            return

        await websocket.accept()

        logger.info(
            f"WebSocket connected for {conversation_id} by {user.email}, after={after}"
        )

        try:
            # Stream events
            async for event in es.stream_events(conversation_id, after=after):
                await websocket.send_json(event.model_dump(mode="json"))
        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected for {conversation_id}")
        except Exception as e:
            logger.error(f"WebSocket error for {conversation_id}: {e}")
            await websocket.close(code=1011, reason="Internal error")
