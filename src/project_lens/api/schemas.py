"""HTTP request and response schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from project_lens.context.models import TimeRange
from project_lens.context.source_records import FactType, SourceRecord
from project_lens.domain.feishu_doc_sync import FeishuDocSyncStatus
from project_lens.application.connector_sync_status import ConnectorSyncState
from project_lens.application.wiki_compiler import WikiPageDraft, WikiPageType
from project_lens.domain.models import (
    AgentRun,
    ChangeImpact,
    KnowledgeGapReport,
    ProjectRef,
    ProjectSnapshot,
    RunStatus,
    TimelineEvent,
)
from project_lens.runtime.events import AgentEvent, AgentEventType


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    channel_id: str | None = Field(default=None, max_length=200)
    question: str = Field(min_length=1, max_length=20_000)


class CreateRunResponse(BaseModel):
    run_id: UUID
    trace_id: UUID
    status: RunStatus

    @classmethod
    def from_run(cls, run: AgentRun) -> "CreateRunResponse":
        return cls(run_id=run.id, trace_id=run.trace_id, status=run.status)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class ReadinessResponse(BaseModel):
    status: str
    service: str
    version: str
    ready: bool
    checks: dict[str, object] = Field(default_factory=dict)


class ProjectSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()


class ProjectSnapshotResponse(ProjectSnapshot):
    pass


class ProjectTimelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    time_range: TimeRange | None = None
    limit: int = Field(default=20, ge=1, le=50)


class ProjectTimelineResponse(BaseModel):
    events: tuple[TimelineEvent, ...] = ()


class ProjectChangeImpactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    time_range: TimeRange | None = None
    limit: int = Field(default=20, ge=1, le=50)


class ProjectChangeImpactResponse(ChangeImpact):
    pass


class ProjectKnowledgeGapsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()


class ProjectKnowledgeGapsResponse(KnowledgeGapReport):
    pass


class ProjectFactResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    fact_type: FactType


class SourceRecordResponse(BaseModel):
    source_id: str
    source_type: str
    project_id: str
    title: str
    raw_uri: str
    revision: str
    observed_at: datetime
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    owner: str | None = None
    status: str
    topic: str
    authority_scope: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    related_sources: tuple[str, ...] = ()
    access_scope: str
    content_hash: str
    evidence_refs: tuple[str, ...] = ()

    @classmethod
    def from_source(cls, source: SourceRecord) -> "SourceRecordResponse":
        return cls.model_validate(source.model_dump())


class ProjectFactResolutionResponse(BaseModel):
    fact_type: FactType
    selected: SourceRecordResponse | None = None
    candidates: tuple[SourceRecordResponse, ...] = ()
    conflicts: tuple[SourceRecordResponse, ...] = ()
    reason: str


class SourceGapResponse(BaseModel):
    status: str
    fact_type: FactType
    project_id: str
    reason: str
    evidence_ids: tuple[str, ...] = ()
    suggested_source_type: str | None = None


class ProjectSourceGapsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()


class ProjectSourceGapsResponse(BaseModel):
    project: ProjectRef
    gaps: tuple[SourceGapResponse, ...] = ()


class ProjectSourceRecordsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    source_id: str | None = Field(default=None, max_length=500)
    include_revoked: bool = False


class ProjectSourceRecordsResponse(BaseModel):
    project: ProjectRef
    records: tuple[SourceRecordResponse, ...] = ()


class ConnectorSyncStatusResponse(BaseModel):
    connector: str
    cursor: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    error_count: int = 0
    last_error: str | None = None
    indexed_count: int = 0
    failed_count: int = 0
    stale: bool = False

    @classmethod
    def from_state(cls, state: ConnectorSyncState, *, stale: bool) -> "ConnectorSyncStatusResponse":
        return cls(
            connector=state.connector,
            cursor=state.cursor.token if state.cursor else None,
            last_attempt_at=state.last_attempt_at,
            last_success_at=state.last_success_at,
            error_count=state.error_count,
            last_error=state.last_error,
            indexed_count=state.indexed_count,
            failed_count=state.failed_count,
            stale=stale,
        )


class ProjectConnectorSyncStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()


class ProjectConnectorSyncStatusResponse(BaseModel):
    project: ProjectRef
    statuses: tuple[ConnectorSyncStatusResponse, ...] = ()


class ProjectConnectorSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()


class ConnectorSyncResultResponse(BaseModel):
    connector: str
    ok: bool
    added_count: int = 0
    updated_count: int = 0
    revoked_count: int = 0
    indexed_count: int = 0
    failed_count: int = 0
    cursor: str | None = None
    stale: bool = False
    freshness_warning: str | None = None
    error: str | None = None
    knowledge_indexed_count: int = 0
    knowledge_index_error: str | None = None


class ProjectConnectorSyncResponse(BaseModel):
    project: ProjectRef
    results: tuple[ConnectorSyncResultResponse, ...] = ()


class ProjectKnowledgeIndexSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    mode: Literal["incremental", "full"] = "incremental"
    dry_run: bool = False


class ProjectKnowledgeIndexStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()


class ProjectKnowledgeIndexStatusResponse(BaseModel):
    project: ProjectRef
    index_stats: dict[str, int] = Field(default_factory=dict)


class ProjectKnowledgeIndexSyncResponse(BaseModel):
    project: ProjectRef
    mode: str
    dry_run: bool
    batches: int
    input_records: int
    documents_upserted: int
    chunks_written: int
    chunks_deactivated: int
    deactivated_documents: int
    embedding_completed: int = 0
    embedding_failed: int = 0
    embedding_cache_hits: int = 0
    index_stats: dict[str, int] = Field(default_factory=dict)


class ProjectWikiDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    compile: bool = True


class WikiPageDraftResponse(BaseModel):
    tenant_id: str
    project_id: str
    page_type: WikiPageType
    title: str
    status: str
    content: str
    source_keys: tuple[str, ...] = ()
    generated_at: datetime

    @classmethod
    def from_draft(cls, draft: WikiPageDraft) -> "WikiPageDraftResponse":
        return cls(
            tenant_id=draft.tenant_id,
            project_id=draft.project_id,
            page_type=draft.page_type,
            title=draft.title,
            status=draft.status,
            content=draft.content,
            source_keys=draft.source_keys,
            generated_at=draft.generated_at,
        )


class ProjectWikiDraftResponse(BaseModel):
    project: ProjectRef
    pages: tuple[WikiPageDraftResponse, ...] = ()


class ProjectWikiReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    page_type: WikiPageType
    approved: bool


class ProjectWikiReviewResponse(BaseModel):
    project: ProjectRef
    page: WikiPageDraftResponse
    published_uri: str | None = None


class ProjectObsidianExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    include_sources: bool = True
    include_review: bool = True


class ProjectObsidianExportResponse(BaseModel):
    project: ProjectRef
    export_id: str
    exported_paths: tuple[str, ...] = ()
    skipped_paths: tuple[str, ...] = ()
    source_count: int = 0
    wiki_count: int = 0
    review_count: int = 0
    conflicted_paths: tuple[str, ...] = ()
    generated_at: datetime


class ProjectKnowledgeOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()
    compile_wiki: bool = True
    export_reviewed: bool = True
    dry_run: bool = False


class ProjectKnowledgeOperationResponse(BaseModel):
    id: str
    operation_type: str
    project: ProjectRef
    requested_by: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    summary: dict[str, object] | None = None
    error: str | None = None
    audit_refs: tuple[str, ...] = ()


class FeishuDocsSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()
    doc_tokens: tuple[str, ...] = ()


class FeishuDocsSyncResponse(BaseModel):
    project_id: str
    requested: int
    fetched: int
    indexed: int
    skipped_unchanged: int
    failed: tuple[str, ...] = ()
    statuses: tuple[FeishuDocSyncStatus, ...] = ()


class FeishuDocsSyncStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectRef
    user_id: str = Field(min_length=1, max_length=100)
    permissions: tuple[str, ...] = ()


class FeishuDocsSyncStatusResponse(BaseModel):
    project: ProjectRef
    statuses: tuple[FeishuDocSyncStatus, ...] = ()
    success_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0


class AgentEventResponse(BaseModel):
    run_id: UUID
    trace_id: UUID
    type: AgentEventType
    occurred_at: datetime
    payload: dict[str, Any]

    @classmethod
    def from_event(cls, event: AgentEvent) -> "AgentEventResponse":
        return cls(
            run_id=event.run_id,
            trace_id=event.trace_id,
            type=event.type,
            occurred_at=event.occurred_at,
            payload=event.payload,
        )
