"""
Shared in-memory conversation state tracker.

Tracks multi-step conversational flows per user so cogs can maintain
context across multiple messages without a database.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConversationState:
    phase: str
    channel_id: int
    guild_id: int
    data: dict[str, Any] = field(default_factory=dict)


class ConversationManager:
    """Thread-safe (asyncio-safe) conversation state store."""

    def __init__(self) -> None:
        self._states: dict[int, ConversationState] = {}

    def start(
        self,
        user_id: int,
        phase: str,
        channel_id: int,
        guild_id: int,
        **data: Any,
    ) -> None:
        self._states[user_id] = ConversationState(
            phase=phase,
            channel_id=channel_id,
            guild_id=guild_id,
            data=dict(data),
        )

    def get(self, user_id: int) -> ConversationState | None:
        return self._states.get(user_id)

    def advance(self, user_id: int, new_phase: str, **extra_data: Any) -> None:
        state = self._states.get(user_id)
        if state:
            state.phase = new_phase
            state.data.update(extra_data)

    def end(self, user_id: int) -> None:
        self._states.pop(user_id, None)

    def is_active_in(self, user_id: int, channel_id: int) -> bool:
        state = self._states.get(user_id)
        return state is not None and state.channel_id == channel_id
