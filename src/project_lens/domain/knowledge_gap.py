"""Persistent knowledge-gap case contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import re
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_lens.domain.models import ProjectRef


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class KnowledgeGapCaseType(StrEnum):
    MISSING_SOURCE = "missing_source"
    CONFLICTING_SOURCES = "conflicting_sources"
    STALE_SOURCE = "stale_source"
    MISSING_OWNER = "missing_owner"
    MISSING_DECISION = "missing_decision"
    MISSING_SCHEDULE = "missing_schedule"
    MISSING_ACCEPTANCE = "missing_acceptance"
    ACCESS_LIMITED = "access_limited"
    UNCLEAR_QUESTION = "unclear_question"


class KnowledgeGapStatus(StrEnum):
    CANDIDATE = "candidate"
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    ASSIGNED = "assigned"
    WAITING_FOR_SOURCE = "waiting_for_source"
    RESOLVED_PENDING_CHECK = "resolved_pending_check"
    RESOLVED = "resolved"
    SNOOZED = "snoozed"
    DISMISSED = "dismissed"
    REOPENED = "reopened"


class GapSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class GapTriggerType(StrEnum):
    ANSWER = "answer"
    SCHEDULED_SCAN = "scheduled_scan"
    WIKI_COMPILE = "wiki_compile"
    SYNC = "sync"


class KnowledgeGapCandidate(FrozenModel):
    """Untrusted detection proposal; caller supplies its authorized evidence set."""

    project: ProjectRef
    type: KnowledgeGapCaseType
    title: str = Field(min_length=1, max_length=300)
    question: str = Field(min_length=1, max_length=2_000)
    description: str = Field(min_length=1, max_length=4_000)
    fact_type: str = Field(default="", max_length=200)
    impact: str = Field(default="", max_length=2_000)
    target_ref: str = Field(default="", max_length=500)
    suggested_source_type: str = Field(default="", max_length=100)
    severity: GapSeverity = GapSeverity.MEDIUM
    evidence_ids: tuple[UUID, ...] = ()
    evidence_status: str = Field(default="authorized", pattern="^(authorized|no_authorized_evidence)$")
    source_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    conflicting_source_ids: tuple[str, ...] = ()
    suggested_owner_ids: tuple[str, ...] = ()


class KnowledgeGapCase(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project: ProjectRef
    canonical_key: str = Field(min_length=64, max_length=64)
    type: KnowledgeGapCaseType
    title: str = Field(min_length=1, max_length=300)
    question: str = Field(min_length=1, max_length=2_000)
    description: str = Field(min_length=1, max_length=4_000)
    fact_type: str = ""
    status: KnowledgeGapStatus = KnowledgeGapStatus.CANDIDATE
    severity: GapSeverity = GapSeverity.MEDIUM
    impact: str = ""
    target_ref: str = ""
    evidence_ids: tuple[UUID, ...] = ()
    evidence_status: str = Field(default="authorized", pattern="^(authorized|no_authorized_evidence)$")
    source_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    conflicting_source_ids: tuple[str, ...] = ()
    suggested_source_type: str = ""
    suggested_owner_ids: tuple[str, ...] = ()
    assigned_owner_ids: tuple[str, ...] = ()
    first_detected_at: datetime
    last_seen_at: datetime
    occurrence_count: int = Field(default=1, ge=1)
    affected_question_count: int = Field(default=1, ge=1)
    resolution_note: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    dismissed_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_lifecycle_fields(self) -> "KnowledgeGapCase":
        if self.last_seen_at < self.first_detected_at:
            raise ValueError("last_seen_at must not precede first_detected_at")
        if (self.status == KnowledgeGapStatus.RESOLVED) != (self.resolved_at is not None):
            raise ValueError("resolved_at is required only for resolved cases")
        if self.status == KnowledgeGapStatus.DISMISSED and not self.dismissed_reason:
            raise ValueError("dismissed cases require dismissed_reason")
        return self


class KnowledgeGapObservation(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    gap_id: UUID
    run_id: str | None = None
    project: ProjectRef
    observed_at: datetime
    trigger_type: GapTriggerType
    question_hash: str = Field(min_length=64, max_length=64)
    evidence_ids: tuple[UUID, ...] = ()
    signal: str = ""
    visibility_scope: str = Field(min_length=1, max_length=500)
    created_by: str = Field(min_length=1, max_length=100)


class KnowledgeGapStateEvent(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    gap_id: UUID
    from_status: KnowledgeGapStatus
    to_status: KnowledgeGapStatus
    actor_id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    reason: str = Field(min_length=1, max_length=2_000)


class GapActionType(StrEnum):
    ADD_SOURCE = "add_source"
    CONFIRM_DECISION = "confirm_decision"
    ASSIGN_OWNER = "assign_owner"
    UPDATE_WIKI = "update_wiki"
    LINK_TASK = "link_task"


class GapActionStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    REJECTED = "rejected"


class KnowledgeGapAction(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    gap_id: UUID
    action_type: GapActionType
    proposed_by: str = Field(min_length=1, max_length=200)
    target_owner_id: str | None = None
    description: str = Field(min_length=1, max_length=2_000)
    status: GapActionStatus = GapActionStatus.PROPOSED
    linked_source_ids: tuple[UUID, ...] = ()
    linked_task_id: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None
    status_reason: str | None = None


class KnowledgeGapRecheck(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    gap_id: UUID
    project: ProjectRef
    trigger: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=200)
    old_status: KnowledgeGapStatus
    new_status: KnowledgeGapStatus
    evidence_ids: tuple[UUID, ...] = ()
    outcome: str = Field(min_length=1, max_length=2_000)
    created_at: datetime


class KnowledgeGapRecheckJob(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(min_length=1, max_length=500)
    gap_id: UUID
    project: ProjectRef
    source_ref: str = Field(min_length=1, max_length=500)
    revision: str = Field(min_length=1, max_length=100)
    status: str = Field(default="pending", pattern="^(pending|running|failed|completed)$")
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime


def normalize_key_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", " ", value)
    value = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value)
    return " ".join(value.split())


def stable_knowledge_gap_key(candidate: KnowledgeGapCandidate) -> str:
    question = normalize_key_text(candidate.question)
    target = normalize_key_text(candidate.target_ref)
    intent = question if target else f"{normalize_key_text(candidate.fact_type)}|{question}"
    raw = "|".join((
        candidate.project.tenant_id,
        candidate.project.project_id,
        candidate.type.value,
        normalize_key_text(candidate.fact_type),
        target,
        intent,
    ))
    return sha256(raw.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


ALLOWED_GAP_TRANSITIONS: dict[KnowledgeGapStatus, frozenset[KnowledgeGapStatus]] = {
    KnowledgeGapStatus.CANDIDATE: frozenset({KnowledgeGapStatus.OPEN, KnowledgeGapStatus.DISMISSED}),
    KnowledgeGapStatus.OPEN: frozenset({KnowledgeGapStatus.ACKNOWLEDGED, KnowledgeGapStatus.SNOOZED, KnowledgeGapStatus.DISMISSED}),
    KnowledgeGapStatus.ACKNOWLEDGED: frozenset({KnowledgeGapStatus.ASSIGNED, KnowledgeGapStatus.SNOOZED, KnowledgeGapStatus.DISMISSED}),
    KnowledgeGapStatus.ASSIGNED: frozenset({KnowledgeGapStatus.WAITING_FOR_SOURCE, KnowledgeGapStatus.RESOLVED_PENDING_CHECK, KnowledgeGapStatus.SNOOZED, KnowledgeGapStatus.DISMISSED}),
    KnowledgeGapStatus.WAITING_FOR_SOURCE: frozenset({KnowledgeGapStatus.RESOLVED_PENDING_CHECK, KnowledgeGapStatus.SNOOZED, KnowledgeGapStatus.DISMISSED}),
    KnowledgeGapStatus.RESOLVED_PENDING_CHECK: frozenset({KnowledgeGapStatus.RESOLVED, KnowledgeGapStatus.REOPENED}),
    KnowledgeGapStatus.RESOLVED: frozenset({KnowledgeGapStatus.REOPENED}),
    KnowledgeGapStatus.SNOOZED: frozenset({KnowledgeGapStatus.OPEN}),
    KnowledgeGapStatus.DISMISSED: frozenset(),
    KnowledgeGapStatus.REOPENED: frozenset({KnowledgeGapStatus.ACKNOWLEDGED, KnowledgeGapStatus.ASSIGNED, KnowledgeGapStatus.WAITING_FOR_SOURCE, KnowledgeGapStatus.SNOOZED, KnowledgeGapStatus.DISMISSED, KnowledgeGapStatus.RESOLVED_PENDING_CHECK}),
}
