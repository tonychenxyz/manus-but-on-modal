"""Memory Curator for Agent Home.

Periodically processes events to:
- Extract and categorize facts
- Update preferences
- Create self-prompts for follow-up
- Tag events for better retrieval
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anthropic

from shared.config import get_settings
from shared.models import Event, EventType

if TYPE_CHECKING:
    from agent_home.persistence import EventStore

logger = logging.getLogger(__name__)


CURATOR_SYSTEM_PROMPT = """You are the Memory Curator for an AI agent system.

Your job is to analyze recent events and extract valuable information to remember.

You should:
1. Identify important facts, decisions, and preferences
2. Categorize information by topic/project
3. Create self-prompts for things that need follow-up
4. Update existing memory files with new information

Be selective - only remember things that will be useful in future conversations.
Focus on:
- User preferences and working style
- Project-specific knowledge
- Recurring patterns and issues
- Decisions and their rationale
- Important outcomes (PRs merged, bugs fixed, etc.)

Output your findings as structured JSON."""


class MemoryCurator:
    """Curates and organizes agent memory.

    Runs periodically to:
    - Process new events
    - Extract facts and preferences
    - Create self-prompts
    - Update memory files
    """

    def __init__(
        self,
        event_store: "EventStore",
        memory_path: Path,
    ):
        """Initialize the curator.

        Args:
            event_store: Event store for reading events
            memory_path: Path to memory directory
        """
        self.event_store = event_store
        self.memory_path = memory_path

        settings = get_settings()
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.orchestrator_model
        self.batch_size = settings.curator_batch_size
        self.interval = settings.curator_interval_seconds

        self._last_cursor = 0
        self._running = False

    async def start(self) -> None:
        """Start the curator background loop."""
        if self._running:
            return

        self._running = True

        # Load last processed cursor
        await self._load_state()

        logger.info(f"Memory Curator starting from cursor {self._last_cursor}")

        while self._running:
            try:
                await self._curate()
            except Exception as e:
                logger.error(f"Curation failed: {e}")

            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        """Stop the curator."""
        self._running = False

    async def curate_now(self) -> dict[str, Any]:
        """Run curation immediately.

        Returns:
            Summary of what was processed
        """
        return await self._curate()

    async def _curate(self) -> dict[str, Any]:
        """Process new events and update memory.

        Returns:
            Summary of processing
        """
        # Get new events
        events = await self.event_store.get_events_since(
            after=self._last_cursor,
            limit=self.batch_size,
        )

        if not events:
            return {"processed": 0, "message": "No new events"}

        logger.info(f"Curating {len(events)} events")

        # Prepare events for analysis
        events_text = self._format_events_for_analysis(events)

        # Get current memory state
        facts = self._read_memory_file("facts.md")
        preferences = self._read_memory_file("preferences.md")

        # Ask Claude to analyze
        prompt = f"""Analyze these recent events and extract information to remember.

## Current Facts
{facts}

## Current Preferences
{preferences}

## New Events
{events_text}

Please provide:
1. New facts to add (if any)
2. Preference updates (if any)
3. Self-prompts for follow-up (if any)
4. Event tags for categorization

Respond with JSON in this format:
{{
    "new_facts": ["fact 1", "fact 2"],
    "preference_updates": ["preference 1"],
    "self_prompts": [
        {{"topic": "topic", "content": "what to follow up on", "priority": "normal"}}
    ],
    "event_tags": [
        {{"cursor": 123, "tags": ["tag1", "tag2"]}}
    ],
    "summary": "Brief summary of what was learned"
}}

If nothing notable, return empty arrays."""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=CURATOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            # Parse response
            result_text = ""
            for block in response.content:
                if block.type == "text":
                    result_text = block.text
                    break

            # Extract JSON from response
            result = self._parse_json_response(result_text)

            # Apply updates
            await self._apply_curation_results(result)

            # Update cursor
            self._last_cursor = events[-1].cursor
            await self._save_state()

            return {
                "processed": len(events),
                "new_facts": len(result.get("new_facts", [])),
                "preference_updates": len(result.get("preference_updates", [])),
                "self_prompts": len(result.get("self_prompts", [])),
                "summary": result.get("summary", ""),
            }

        except Exception as e:
            logger.error(f"Curation analysis failed: {e}")
            # Still update cursor to avoid reprocessing
            self._last_cursor = events[-1].cursor
            await self._save_state()
            return {"processed": len(events), "error": str(e)}

    def _format_events_for_analysis(self, events: list[Event]) -> str:
        """Format events for Claude analysis.

        Args:
            events: Events to format

        Returns:
            Formatted text
        """
        lines = []
        for event in events:
            timestamp = event.ts.isoformat()
            event_type = event.type.value

            # Format payload based on event type
            if event.type in (EventType.MESSAGE_USER, EventType.MESSAGE_ASSISTANT):
                content = event.payload.get("content", "")[:500]
                lines.append(f"[{timestamp}] {event_type}: {content}")

            elif event.type == EventType.RUN_COMPLETED:
                lines.append(f"[{timestamp}] {event_type}: Run {event.run_id} completed")

            elif event.type == EventType.RUN_FAILED:
                error = event.payload.get("error", "unknown")
                lines.append(f"[{timestamp}] {event_type}: Run {event.run_id} failed: {error}")

            elif event.type == EventType.JOB_COMPLETED:
                job_id = event.job_id
                result = event.payload.get("result", {})
                pr_url = result.get("pr_url", "")
                summary = result.get("summary", "")[:200]
                lines.append(
                    f"[{timestamp}] {event_type}: Job {job_id}"
                    f"{' PR: ' + pr_url if pr_url else ''}"
                    f"{' - ' + summary if summary else ''}"
                )

            else:
                lines.append(f"[{timestamp}] {event_type}")

        return "\n".join(lines)

    def _parse_json_response(self, text: str) -> dict[str, Any]:
        """Parse JSON from Claude's response.

        Args:
            text: Response text

        Returns:
            Parsed JSON dict
        """
        # Try to find JSON in the response
        start = text.find("{")
        end = text.rfind("}") + 1

        if start == -1 or end == 0:
            return {}

        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            logger.warning("Failed to parse curation JSON")
            return {}

    async def _apply_curation_results(self, result: dict[str, Any]) -> None:
        """Apply curation results to memory.

        Args:
            result: Parsed curation results
        """
        # Add new facts
        new_facts = result.get("new_facts", [])
        if new_facts:
            self._append_to_memory_file("facts.md", new_facts)

        # Update preferences
        pref_updates = result.get("preference_updates", [])
        if pref_updates:
            self._append_to_memory_file("preferences.md", pref_updates)

        # Create self-prompts
        self_prompts = result.get("self_prompts", [])
        for prompt in self_prompts:
            await self._create_self_prompt(prompt)

        # Store event tags (for future retrieval optimization)
        event_tags = result.get("event_tags", [])
        if event_tags:
            self._store_event_tags(event_tags)

    def _read_memory_file(self, filename: str) -> str:
        """Read a memory file.

        Args:
            filename: File name within memory directory

        Returns:
            File contents or empty string
        """
        path = self.memory_path / filename
        if path.exists():
            return path.read_text()
        return ""

    def _append_to_memory_file(self, filename: str, items: list[str]) -> None:
        """Append items to a memory file.

        Args:
            filename: File name within memory directory
            items: Items to append
        """
        path = self.memory_path / filename

        # Read existing content
        existing = ""
        if path.exists():
            existing = path.read_text()

        # Append new items
        timestamp = datetime.utcnow().strftime("%Y-%m-%d")
        new_content = f"\n\n## Added {timestamp}\n\n"
        new_content += "\n".join(f"- {item}" for item in items)

        path.write_text(existing + new_content)

    async def _create_self_prompt(self, prompt_data: dict[str, Any]) -> None:
        """Create a self-prompt file.

        Args:
            prompt_data: Dict with topic, content, priority
        """
        inbox_path = self.memory_path / "inbox"
        inbox_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        topic = prompt_data.get("topic", "general").replace(" ", "_").lower()[:30]
        filename = f"{timestamp}__{topic}.md"

        content = f"""# Self-Prompt: {prompt_data.get('topic', 'General')}

**Created:** {datetime.utcnow().isoformat()}
**Priority:** {prompt_data.get('priority', 'normal')}
**Source:** Memory Curator

## Content

{prompt_data.get('content', '')}
"""

        (inbox_path / filename).write_text(content)

    def _store_event_tags(self, event_tags: list[dict[str, Any]]) -> None:
        """Store event tags for future retrieval.

        Args:
            event_tags: List of {cursor, tags} dicts
        """
        tags_path = self.memory_path / "tags"
        tags_path.mkdir(parents=True, exist_ok=True)

        # Group by tag
        by_tag: dict[str, list[int]] = {}
        for item in event_tags:
            cursor = item.get("cursor")
            for tag in item.get("tags", []):
                if tag not in by_tag:
                    by_tag[tag] = []
                by_tag[tag].append(cursor)

        # Append to tag files
        for tag, cursors in by_tag.items():
            tag_file = tags_path / f"{tag}.json"

            existing: list[int] = []
            if tag_file.exists():
                try:
                    existing = json.loads(tag_file.read_text())
                except json.JSONDecodeError:
                    pass

            # Add new cursors and dedupe
            all_cursors = sorted(set(existing + cursors))
            tag_file.write_text(json.dumps(all_cursors))

    async def _load_state(self) -> None:
        """Load curator state from disk."""
        state_file = self.memory_path / "curator_state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                self._last_cursor = state.get("last_cursor", 0)
            except json.JSONDecodeError:
                pass

    async def _save_state(self) -> None:
        """Save curator state to disk."""
        state_file = self.memory_path / "curator_state.json"
        state = {
            "last_cursor": self._last_cursor,
            "last_run": datetime.utcnow().isoformat(),
        }
        state_file.write_text(json.dumps(state, indent=2))
