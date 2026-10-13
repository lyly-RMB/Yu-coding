"""Idempotency storage for Feishu event callbacks."""

from __future__ import annotations

from typing import Protocol


class EventDeduplicator(Protocol):
    def mark_seen(self, event_id: str) -> bool: ...
