"""Core domain models shared by retrieval, agent runtime, and response renderers."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceType(StrEnum):
    DOCUMENT = "document"
    CODE = "code"
    COMMIT = "commit"
    PULL_REQUEST = "pull_request"
    LOG = "log"
    TASK = "task"
    INCIDENT = "incident"
    METRIC = "metric"


class EvidenceGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    UNKNOWN = "unknown"


class ClaimType(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    ACCEPTED = "accepted"
    RESOLVING = "resolving"
    COLLECTING = "collecting"
    ANALYZING = "analyzing"
    VERIFYING = "verifying"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class ProjectRef(FrozenModel):
    tenant_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    service: str | None = Field(default=None, max_length=150)
    environment: str | None = Field(default=None, max_length=50)


class SourceRef(FrozenModel):
    system: str = Field(min_length=1, max_length=50)
    source_id: str = Field(min_length=1, max_length=500)
    url: str | None = None


class Evidence(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    type: EvidenceType
    project: ProjectRef
    source: SourceRef
    content: str = Field(min_length=1)
    observed_at: datetime
    access_scope: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(min_length=16, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    revoked: bool = False
    revoked_at: datetime | None = None


class EvidenceRef(FrozenModel):
    """Citation-safe evidence reference; never carries the source body."""

    id: UUID
    kind: EvidenceType
    source_uri: str = Field(min_length=1, max_length=600)
    summary: str = Field(default="", max_length=300)


class Claim(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    text: str = Field(min_length=1)
    type: ClaimType
    evidence_ids: tuple[UUID, ...] = ()
    grade: EvidenceGrade = EvidenceGrade.UNKNOWN

    @model_validator(mode="after")
    def unknown_grade_constraint(self) -> "Claim":
        # FACT may omit evidence_ids: Feishu answers still post the body and
        # separately state which sources were cited (or that none were).
        if self.type == ClaimType.UNKNOWN and self.grade != EvidenceGrade.UNKNOWN:
            raise ValueError("unknown claims must use the unknown evidence grade")
        if self.type == ClaimType.FACT and self.grade in {
            EvidenceGrade.A,
            EvidenceGrade.B,
        } and not self.evidence_ids:
            raise ValueError("verified fact claims require evidence")
        return self


class ActionProposal(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    tool_name: str | None = None
    requires_approval: bool = True
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProjectSnapshot(FrozenModel):
    project: ProjectRef
    services: tuple[str, ...] = ()
    entrypoints: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    generated_at: datetime = Field(default_factory=utc_now)


class TimelineEvent(FrozenModel):
    project: ProjectRef
    event_type: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    observed_at: datetime
    source: SourceRef
    evidence_id: UUID
    summary: str = Field(default="", max_length=1_000)
    references: tuple[str, ...] = ()


class ChangeImpact(FrozenModel):
    """Structured, read-only summary of changes observed in project evidence."""

    project: ProjectRef
    summary: str = Field(default="", max_length=2_000)
    affected_services: tuple[str, ...] = ()
    related_events: tuple[TimelineEvent, ...] = ()
    risks: tuple[str, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    generated_at: datetime = Field(default_factory=utc_now)


class KnowledgeGapType(StrEnum):
    ARCHITECTURE = "architecture"
    OWNER = "owner"
    VERSION_HISTORY = "version_history"
    TASK_TRACKING = "task_tracking"
    INCIDENT_REVIEW = "incident_review"
    CODE_COVERAGE = "code_coverage"
    DECISION_RECORD = "decision_record"
    ACCESS_LIMITED = "access_limited"
    API_DOC = "api_doc"
    RELEASE_HISTORY = "release_history"
    RUNBOOK = "runbook"


class KnowledgeGapSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class KnowledgeGap(FrozenModel):
    type: KnowledgeGapType
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    recommendation: str = Field(default="", max_length=1_000)
    severity: KnowledgeGapSeverity = KnowledgeGapSeverity.MEDIUM
    source_signal: str = Field(min_length=1, max_length=200)
    evidence_ids: tuple[UUID, ...] = ()
    # Entity-aware gap targeting (service/module/incident/project).
    target_ref: str | None = Field(default=None, max_length=200)
    suggested_owners: tuple[str, ...] = ()
    # Optional GraphEvidence path summaries used when the gap is graph-derived.
    graph_summaries: tuple[str, ...] = ()


class KnowledgeGapReport(FrozenModel):
    """Structured, ACL-filtered gaps in project knowledge coverage."""

    project: ProjectRef
    gaps: tuple[KnowledgeGap, ...] = ()
    type_coverage: dict[str, int] = Field(default_factory=dict)
    # Theme-level coverage signals (0/1): owner/api_doc/runbook/release/...
    signal_coverage: dict[str, int] = Field(default_factory=dict)
    evidence_ids: tuple[UUID, ...] = ()
    generated_at: datetime = Field(default_factory=utc_now)


class GraphEvidence(FrozenModel):
    """Explainable graph path anchored to source evidence ids."""

    project: ProjectRef
    path_labels: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    node_ids: tuple[str, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    summary: str = Field(default="", max_length=1_000)
    # Optional bodies so analysis can cite graph anchors outside lexical hits.
    cited_evidence: tuple[Evidence, ...] = ()


class ProjectAnswer(FrozenModel):
    project: ProjectRef
    skill: str = Field(default="project_knowledge", min_length=1, max_length=100)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: str
    # Deprecated compatibility projections. New code should use answer_text
    # for presentation and claims/evidence for verification.
    business_summary: str = Field(
        default="",
        description="Deprecated presentation fallback; use answer_text.",
    )
    technical_summary: str = Field(
        default="",
        description="Deprecated technical presentation fallback; use answer_text or evidence.",
    )
    conclusion: str = Field(
        default="",
        description="Deprecated presentation fallback; use answer_text.",
    )
    claims: tuple[Claim, ...] = ()
    facts: tuple[Claim, ...] = ()
    inferences: tuple[Claim, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    impact: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    citations: tuple[EvidenceRef, ...] = ()
    policy_used: str = "unknown"
    unknowns: tuple[str, ...] = ()
    recommended_actions: tuple[ActionProposal, ...] = ()
    # Complete Hermes-facing answer text. Evidence fields below remain the
    # verified fact projection and must not replace this正文.
    answer_text: str = ""

    @model_validator(mode="after")
    def claims_reference_known_evidence(self) -> "ProjectAnswer":
        known = {item.id for item in self.evidence}
        for item in self.evidence:
            if item.project != self.project:
                raise ValueError("evidence belongs to a different project")
            if not item.access_scope.startswith(f"project:{self.project.project_id}:"):
                raise ValueError("evidence is outside the project access scope")
        missing = {
            evidence_id
            for claim in self.claims
            for evidence_id in claim.evidence_ids
            if evidence_id not in known
        }
        if missing:
            raise ValueError(f"claims reference missing evidence: {sorted(map(str, missing))}")
        if any(item.type != ClaimType.FACT for item in self.facts):
            raise ValueError("ProjectAnswer.facts may contain only fact claims")
        if any(item.type != ClaimType.INFERENCE for item in self.inferences):
            raise ValueError("ProjectAnswer.inferences may contain only inference claims")
        claim_ids = {item.id for item in self.claims}
        if any(item.id not in claim_ids for item in (*self.facts, *self.inferences)):
            raise ValueError("facts and inferences must reference claims from the fact core")
        citation_ids = {item.id for item in self.citations}
        if not citation_ids.issubset(known):
            raise ValueError("citations must reference evidence carried by the answer")
        return self


class AgentRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    channel_id: str | None = Field(default=None, max_length=200)
    question: str = Field(min_length=1, max_length=20_000)
    runtime_access: dict[str, object] | None = None
    # Runtime fields are audit metadata.  The serialized access snapshot remains
    # the authorization source for replay and must be re-bound before use.
    runtime: str = Field(default="legacy", min_length=1, max_length=50)
    entry_mode: str | None = Field(default=None, max_length=80)
    hermes_loop_id: UUID | None = None
    context_snapshot_id: UUID | None = None
    context_hash: str | None = Field(default=None, max_length=128)
    status: RunStatus = RunStatus.ACCEPTED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    answer: ProjectAnswer | None = None
    error: str | None = None
