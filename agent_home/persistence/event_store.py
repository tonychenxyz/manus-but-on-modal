"""Event store for append-only event logging.

Provides the core event persistence layer using SQLite with WAL mode for
concurrent reads and reliable durability.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from shared.models import Event, EventType

logger = logging.getLogger(__name__)


class EventStore:
    """Append-only event store backed by SQLite.

    Features:
    - Monotonically increasing cursor for each event
    - Efficient range queries for catch-up
    - JSON payload storage for flexibility
    - WAL mode for concurrent access
    """

    def __init__(self, db_path: str | Path):
        """Initialize the event store.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = {}

    async def initialize(self) -> None:
        """Initialize the database and create tables if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row

        # Enable WAL mode for better concurrent access
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")

        # Create events table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                run_id TEXT,
                job_id TEXT
            )
        """)

        # Create indexes for efficient queries
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_conversation
            ON events(conversation_id, cursor)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_run
            ON events(run_id, cursor)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(type, cursor)
        """)

        await self._db.commit()
        logger.info(f"Event store initialized at {self.db_path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def append(self, event: Event) -> Event:
        """Append an event to the store.

        Args:
            event: The event to append (cursor will be assigned)

        Returns:
            The event with assigned cursor
        """
        if not self._db:
            raise RuntimeError("Event store not initialized")

        async with self._lock:
            cursor = await self._db.execute(
                """
                INSERT INTO events (ts, conversation_id, type, payload, run_id, job_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.ts.isoformat(),
                    event.conversation_id,
                    event.type.value,
                    json.dumps(event.payload),
                    event.run_id,
                    event.job_id,
                ),
            )
            await self._db.commit()

            # Update event with assigned cursor
            event.cursor = cursor.lastrowid or 0

        # Notify subscribers
        await self._notify_subscribers(event)

        return event

    async def append_many(self, events: list[Event]) -> list[Event]:
        """Append multiple events atomically.

        Args:
            events: Events to append

        Returns:
            Events with assigned cursors
        """
        if not self._db:
            raise RuntimeError("Event store not initialized")

        if not events:
            return []

        async with self._lock:
            for event in events:
                cursor = await self._db.execute(
                    """
                    INSERT INTO events (ts, conversation_id, type, payload, run_id, job_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.ts.isoformat(),
                        event.conversation_id,
                        event.type.value,
                        json.dumps(event.payload),
                        event.run_id,
                        event.job_id,
                    ),
                )
                event.cursor = cursor.lastrowid or 0

            await self._db.commit()

        # Notify subscribers
        for event in events:
            await self._notify_subscribers(event)

        return events

    async def get_events(
        self,
        conversation_id: str,
        after: int = 0,
        limit: int = 200,
        event_types: list[EventType] | None = None,
    ) -> list[Event]:
        """Get events for a conversation.

        Args:
            conversation_id: The conversation to get events for
            after: Return events after this cursor (exclusive)
            limit: Maximum number of events to return
            event_types: Filter by event types (optional)

        Returns:
            List of events ordered by cursor
        """
        if not self._db:
            raise RuntimeError("Event store not initialized")

        query = """
            SELECT cursor, ts, conversation_id, type, payload, run_id, job_id
            FROM events
            WHERE conversation_id = ? AND cursor > ?
        """
        params: list = [conversation_id, after]

        if event_types:
            placeholders = ",".join("?" * len(event_types))
            query += f" AND type IN ({placeholders})"
            params.extend(t.value for t in event_types)

        query += " ORDER BY cursor ASC LIMIT ?"
        params.append(limit)

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        return [self._row_to_event(row) for row in rows]

    async def get_events_for_run(
        self,
        run_id: str,
        after: int = 0,
        limit: int = 200,
    ) -> list[Event]:
        """Get events for a specific run.

        Args:
            run_id: The run to get events for
            after: Return events after this cursor (exclusive)
            limit: Maximum number of events to return

        Returns:
            List of events ordered by cursor
        """
        if not self._db:
            raise RuntimeError("Event store not initialized")

        async with self._db.execute(
            """
            SELECT cursor, ts, conversation_id, type, payload, run_id, job_id
            FROM events
            WHERE run_id = ? AND cursor > ?
            ORDER BY cursor ASC
            LIMIT ?
            """,
            (run_id, after, limit),
        ) as cursor:
            rows = await cursor.fetchall()

        return [self._row_to_event(row) for row in rows]

    async def get_latest_cursor(self) -> int:
        """Get the latest cursor value.

        Returns:
            The highest cursor value, or 0 if no events
        """
        if not self._db:
            raise RuntimeError("Event store not initialized")

        async with self._db.execute(
            "SELECT MAX(cursor) as max_cursor FROM events"
        ) as cursor:
            row = await cursor.fetchone()
            return row["max_cursor"] if row and row["max_cursor"] else 0

    async def get_events_since(
        self,
        after: int = 0,
        limit: int = 200,
    ) -> list[Event]:
        """Get all events since a cursor (across all conversations).

        Useful for curator and global event processing.

        Args:
            after: Return events after this cursor (exclusive)
            limit: Maximum number of events to return

        Returns:
            List of events ordered by cursor
        """
        if not self._db:
            raise RuntimeError("Event store not initialized")

        async with self._db.execute(
            """
            SELECT cursor, ts, conversation_id, type, payload, run_id, job_id
            FROM events
            WHERE cursor > ?
            ORDER BY cursor ASC
            LIMIT ?
            """,
            (after, limit),
        ) as cursor:
            rows = await cursor.fetchall()

        return [self._row_to_event(row) for row in rows]

    def subscribe(self, conversation_id: str) -> asyncio.Queue[Event]:
        """Subscribe to events for a conversation.

        Args:
            conversation_id: The conversation to subscribe to

        Returns:
            A queue that will receive new events
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        if conversation_id not in self._subscribers:
            self._subscribers[conversation_id] = []
        self._subscribers[conversation_id].append(queue)
        return queue

    def unsubscribe(self, conversation_id: str, queue: asyncio.Queue[Event]) -> None:
        """Unsubscribe from conversation events.

        Args:
            conversation_id: The conversation to unsubscribe from
            queue: The queue to remove
        """
        if conversation_id in self._subscribers:
            try:
                self._subscribers[conversation_id].remove(queue)
                if not self._subscribers[conversation_id]:
                    del self._subscribers[conversation_id]
            except ValueError:
                pass

    async def _notify_subscribers(self, event: Event) -> None:
        """Notify subscribers of a new event."""
        subscribers = self._subscribers.get(event.conversation_id, [])
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    f"Subscriber queue full for conversation {event.conversation_id}"
                )

    async def stream_events(
        self,
        conversation_id: str,
        after: int = 0,
    ) -> AsyncIterator[Event]:
        """Stream events for a conversation.

        First yields all events after the cursor, then yields new events as
        they arrive.

        Args:
            conversation_id: The conversation to stream
            after: Start streaming after this cursor

        Yields:
            Events in order
        """
        # First, catch up on existing events
        cursor = after
        while True:
            events = await self.get_events(conversation_id, after=cursor, limit=100)
            if not events:
                break
            for event in events:
                yield event
                cursor = event.cursor

        # Then subscribe to new events
        queue = self.subscribe(conversation_id)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self.unsubscribe(conversation_id, queue)

    def _row_to_event(self, row: aiosqlite.Row) -> Event:
        """Convert a database row to an Event."""
        return Event(
            cursor=row["cursor"],
            ts=datetime.fromisoformat(row["ts"]),
            conversation_id=row["conversation_id"],
            type=EventType(row["type"]),
            payload=json.loads(row["payload"]),
            run_id=row["run_id"],
            job_id=row["job_id"],
        )
