"""Multi-turn conversation session contracts for the Agent Harness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from project_lens.domain.models import ProjectRef, utc_now
from project_lens.domain.session_state import ProjectSessionState

DEFAULT_RECENT_TURN_LIMIT = 6
DEFAULT_SESSION_TTL = timedelta(days=7)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConversationTurn(FrozenModel):
    run_id: UUID | None = None
    user_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20_000)
    rewritten_question: str | None = Field(default=None, max_length=20_000)
    created_at: datetime = Field(default_factory=utc_now)


class PinnedIds(FrozenModel):
    """IDs and paths that must survive L1->L2 compression."""

    run_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    incident_ids: tuple[str, ...] = ()
    commit_shas: tuple[str, ...] = ()
    doc_tokens: tuple[str, ...] = ()
    file_paths: tuple[str, ...] = ()
    symbol_names: tuple[str, ...] = ()


class ConversationSummary(FrozenModel):
    """L2 rolling session summary. Runtime context only — never ProjectMemory."""

    project: ProjectRef
    active_skill: str | None = None
    active_topic: dict[str, str] = Field(default_factory=dict)
    verified_claim_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    unknowns: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    pinned_ids: PinnedIds = Field(default_factory=PinnedIds)
    compression_cycle: int = 0
    session_intent: str | None = Field(default=None, max_length=500)
    last_assistant_summary: str | None = Field(default=None, max_length=500)
    updated_at: datetime = Field(default_factory=utc_now)


class ConversationSession(FrozenModel):
    """Harness session spanning Feishu multi-turn collaboration."""

    session_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=100)
    chat_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=100)
    project: ProjectRef
    last_run_id: UUID | None = None
    recent_turns: tuple[ConversationTurn, ...] = ()
    summary: ConversationSummary
    session_state: ProjectSessionState | None = None
    # L3 scratchpad reserved for later Engineering/Ops process state.
    task_scratchpad: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) + DEFAULT_SESSION_TTL
    )

    @property
    def id(self) -> UUID:
        return self.session_id

    @property
    def project_ref(self) -> ProjectRef:
        return self.project


def empty_summary(project: ProjectRef) -> ConversationSummary:
    return ConversationSummary(project=project)
