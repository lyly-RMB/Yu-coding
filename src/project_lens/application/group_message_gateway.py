"""Port for on-demand Feishu group chat history (S08).

Application depends on this protocol; integrations provide the Feishu client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class GroupChatMessage:
    """One group message normalized for citation / candidate UI."""

    message_id: str
    occurred_at: datetime
    text: str
    sender_id: str | None = None


@dataclass(frozen=True)
class GroupChatHistoryPage:
    """One page of group history, or an explicit unavailable result."""

    items: tuple[GroupChatMessage, ...] = ()
    has_more: bool = False
    next_page_token: str | None = None
    available: bool = True
    unavailable_reason: str | None = None


class GroupChatHistoryGateway(Protocol):
    """Read-only Feishu IM history for a chat container."""

    def list_chat_history(
        self,
        *,
        chat_id: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        page_size: int = 5,
        page_token: str | None = None,
    ) -> GroupChatHistoryPage: ...

    def get_message_text(self, message_id: str) -> str | None: ...
