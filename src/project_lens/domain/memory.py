"""Project memory proposals and approved long-lived memories."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from project_lens.domain.approval import DEFAULT_APPROVAL_TTL
from project_lens.domain.models import ClaimType, FrozenModel, ProjectRef, utc_now


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REVIEW = "needs_review"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class ReviewReason(StrEnum):
    SOURCE_REVISED = "source_revised"
    EVIDENCE_REVOKED = "evidence_revoked"
    VALIDITY_EXPIRED = "validity_expired"
    CONFLICT = "conflict"
    REVIEW_DUE = "review_due"
    USER_FEEDBACK = "user_feedback"
    MANUAL = "manual"


class MemorySourceRef(FrozenModel):
    tenant_id: str
    project_id: str
    source_id: str
    revision: str
    content_hash: str | None = None
    source_type: str | None = None


class MemoryType(StrEnum):
    OWNER = "owner"
    ARCHITECTURE_FACT = "architecture_fact"
    DECISION = "decision"
    RISK = "risk"
    RUNBOOK = "runbook"
    POSTMORTEM = "postmortem"
    BUSINESS_RULE = "business_rule"
    TEAM_CONVENTION = "team_convention"
    REQUIREMENT_CHANGE = "requirement_change"


_MEMORY_TYPE_LABELS = {
    MemoryType.OWNER: "负责人",
    MemoryType.ARCHITECTURE_FACT: "架构事实",
    MemoryType.DECISION: "决策",
    MemoryType.RISK: "风险",
    MemoryType.RUNBOOK: "Runbook",
    MemoryType.POSTMORTEM: "复盘",
    MemoryType.BUSINESS_RULE: "业务规则",
    MemoryType.TEAM_CONVENTION: "团队约定",
    MemoryType.REQUIREMENT_CHANGE: "需求变更",
}


def memory_type_label(memory_type: MemoryType | str) -> str:
    try:
        return _MEMORY_TYPE_LABELS[MemoryType(memory_type)]
    except ValueError:
        return str(memory_type)


def default_memory_proposal_expires_at() -> datetime:
    return utc_now() + DEFAULT_APPROVAL_TTL


def normalize_memory_text(text: str) -> str:
    """Normalize claim text for conflict detection."""

    return "".join(text.casefold().split())


class MemoryProposal(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project: ProjectRef
    proposed_by: str = Field(min_length=1, max_length=100)
    claim_text: str = Field(min_length=1, max_length=2_000)
    claim_type: ClaimType = ClaimType.FACT
    memory_type: MemoryType = MemoryType.ARCHITECTURE_FACT
    evidence_ids: tuple[UUID, ...] = ()
    source_refs: tuple[MemorySourceRef, ...] = ()
    subject: str | None = None
    claim_slot: str | None = None
    authority_scope: tuple[str, ...] = ()
    visibility_scope: tuple[str, ...] = ()
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    review_due_at: datetime | None = None
    content_hash: str | None = None
    reason: str = Field(default="", max_length=2_000)
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    allowed_approvers: tuple[str, ...] = ()
    expires_at: datetime | None = Field(default_factory=default_memory_proposal_expires_at)
    replaces_memory_id: UUID | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    rollback_plan: str | None = Field(
        default="Revoke ProjectMemory.valid_to immediately if the fact is wrong.",
        max_length=2_000,
    )
    created_at: datetime = Field(default_factory=utc_now)


class ProjectMemory(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project: ProjectRef
    text: str = Field(min_length=1, max_length=2_000)
    memory_type: MemoryType = MemoryType.ARCHITECTURE_FACT
    claim_type: ClaimType = ClaimType.FACT
    fact_key: str | None = None
    subject: str | None = None
    evidence_ids: tuple[UUID, ...] = ()
    source_refs: tuple[MemorySourceRef, ...] = ()
    authority_scope: tuple[str, ...] = ()
    visibility_scope: tuple[str, ...] = ()
    approved_by: str = Field(min_length=1, max_length=100)
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime | None = None
    observed_at: datetime | None = None
    recorded_at: datetime = Field(default_factory=utc_now)
    review_due_at: datetime | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    proposal_id: UUID | None = None
    supersedes_memory_id: UUID | None = None
    content_hash: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MemoryObservationKind(StrEnum):
    TOOL_RESULT = "tool_result"
    TOOL_DENIED = "tool_denied"
    STATUS_CHANGE = "status_change"
    RUN_OUTCOME = "run_outcome"


class MemoryObservation(FrozenModel):
    """Historical observation derived from an AgentEvent.

    This is not an approved ProjectMemory and must never be promoted silently.
    """

    id: UUID = Field(default_factory=uuid4)
    project: ProjectRef
    run_id: UUID
    observed_at: datetime
    kind: MemoryObservationKind
    text: str = Field(min_length=1, max_length=1_000)
    tool_name: str | None = Field(default=None, max_length=120)
    event_type: str = Field(min_length=1, max_length=100)
    evidence_ids: tuple[UUID, ...] = ()
    source_event_payload_keys: tuple[str, ...] = ()


class Episode(FrozenModel):
    """Bounded historical summary for one AgentRun."""

    id: UUID = Field(default_factory=uuid4)
    project: ProjectRef
    run_id: UUID
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(default="", max_length=2_000)
    started_at: datetime
    ended_at: datetime
    status: str = Field(min_length=1, max_length=50)
    tool_names: tuple[str, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    observation_ids: tuple[UUID, ...] = ()
