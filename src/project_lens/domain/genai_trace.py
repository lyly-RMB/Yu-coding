"""GenAI trace domain models (OpenTelemetry GenAI instruction-cycle archive)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from project_lens.domain.models import utc_now

TraceEntryKind = Literal[
    "user_instruction",
    "message",
    "ai_reply",
    "tool_result",
    "runtime_state",
    "trace_boundary",
]


class TraceEntry(BaseModel):
    """One timestamped item inside a GenAI trace file."""

    model_config = ConfigDict(extra="forbid")

    kind: TraceEntryKind
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _require_timestamp(cls, value: datetime | None) -> datetime:
        if value is None:
            raise ValueError("trace entry timestamp is required")
        return value


class GenAITrace(BaseModel):
    """Full GenAI instruction-cycle archive written under data/traces."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    trace_id: UUID
    run_id: UUID | None = None
    tenant_id: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=120)
    started_at: datetime
    ended_at: datetime
    user_instruction: str = Field(min_length=1, max_length=20_000)
    messages: tuple[dict[str, Any], ...] = ()
    ai_replies: tuple[dict[str, Any], ...] = ()
    tool_results: tuple[dict[str, Any], ...] = ()
    tools: tuple[dict[str, Any], ...] = ()
    runtime_state: dict[str, Any] = Field(default_factory=dict)
    entries: tuple[TraceEntry, ...] = ()

    @model_validator(mode="after")
    def _require_entry_timestamps(self) -> GenAITrace:
        if not self.entries:
            raise ValueError("genai trace must contain at least one timestamped entry")
        for entry in self.entries:
            if entry.timestamp is None:
                raise ValueError("trace entry timestamp is required")
        if self.started_at is None or self.ended_at is None:
            raise ValueError("trace started_at and ended_at are required")
        return self

    def ordered_entries(self) -> tuple[TraceEntry, ...]:
        return tuple(sorted(self.entries, key=lambda item: item.timestamp))


class GenAITraceIndexRecord(BaseModel):
    """Lightweight SQLite index row pointing at a GenAI trace json file."""

    model_config = ConfigDict(extra="forbid")

    trace_id: UUID
    run_id: UUID | None = None
    tenant_id: str
    project_id: str
    started_at: datetime
    ended_at: datetime
    file_path: str
    indexed_at: datetime = Field(default_factory=utc_now)
