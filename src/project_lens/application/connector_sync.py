"""Apply connector SyncBatch results into EvidenceIndex with durable cursors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from project_lens.application.connector_sync_status import (
    ConnectorSyncState,
    freshness_warning,
    sync_state_is_stale,
)
from project_lens.context.connectors.base import Connector, SyncBatch, SyncCursor
from project_lens.context.source_records import SourceRecord
from project_lens.context.source_store import InMemorySourceRecordStore, SourceRecordStore
from project_lens.context.store import EvidenceIndex
from project_lens.domain.models import Evidence, ProjectRef


class SyncStateStore(Protocol):
    def upsert(self, state: ConnectorSyncState) -> ConnectorSyncState: ...

    def get(
        self,
        connector: str,
        tenant_id: str,
        project_id: str,
    ) -> ConnectorSyncState | None: ...


@dataclass(frozen=True)
class ConnectorSyncResult:
    connector: str
    project: ProjectRef
    ok: bool
    batch: SyncBatch | None
    state: ConnectorSyncState
    added_count: int = 0
    updated_count: int = 0
    revoked_count: int = 0
    stale: bool = False
    freshness_warning: str | None = None
    error: str | None = None
    knowledge_indexed_count: int = 0
    knowledge_index_error: str | None = None


class ConnectorSyncService:
    """Read-only sync orchestrator: never opens Apply / PR / deploy paths."""

    def __init__(
        self,
        *,
        evidence_index: EvidenceIndex,
        state_store: SyncStateStore,
        source_store: SourceRecordStore | None = None,
        freshness_max_age_seconds: int = 86_400,
        memory_review_service=None,
        knowledge_index_sync=None,
    ) -> None:
        self._index = evidence_index
        self._states = state_store
        self._sources = source_store or InMemorySourceRecordStore()
        self._freshness_max_age_seconds = freshness_max_age_seconds
        self._memory_review_service = memory_review_service
        self._knowledge_index_sync = knowledge_index_sync

    def configure_memory_review_service(self, service) -> None:
        self._memory_review_service = service

    def configure_knowledge_index_sync(self, service) -> None:
        self._knowledge_index_sync = service

    async def sync_connector(
        self,
        connector: Connector,
        *,
        project: ProjectRef,
        now: datetime | None = None,
    ) -> ConnectorSyncResult:
        clock = now or datetime.now(timezone.utc)
        prior = self._states.get(
            connector.name,
            project.tenant_id,
            project.project_id,
        )
        attempt = ConnectorSyncState(
            connector=connector.name,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            cursor=prior.cursor if prior is not None else None,
            last_attempt_at=clock,
            last_success_at=prior.last_success_at if prior is not None else None,
            error_count=prior.error_count if prior is not None else 0,
            last_error=prior.last_error if prior is not None else None,
            indexed_count=prior.indexed_count if prior is not None else 0,
            failed_count=prior.failed_count if prior is not None else 0,
        )
        self._states.upsert(attempt)

        try:
            batch = await connector.sync(attempt.cursor)
        except Exception as exc:  # noqa: BLE001 - connector boundary
            failed = ConnectorSyncState(
                connector=connector.name,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                cursor=attempt.cursor,
                last_attempt_at=clock,
                last_success_at=attempt.last_success_at,
                error_count=attempt.error_count + 1,
                last_error=str(exc),
                indexed_count=attempt.indexed_count,
                failed_count=attempt.failed_count + 1,
            )
            self._states.upsert(failed)
            warning = freshness_warning(
                failed,
                max_age_seconds=self._freshness_max_age_seconds,
                now=clock,
            )
            return ConnectorSyncResult(
                connector=connector.name,
                project=project,
                ok=False,
                batch=None,
                state=failed,
                stale=sync_state_is_stale(
                    failed,
                    max_age_seconds=self._freshness_max_age_seconds,
                    now=clock,
                ),
                freshness_warning=warning,
                error=str(exc),
            )

        if batch.errors:
            failed = ConnectorSyncState(
                connector=connector.name,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                cursor=attempt.cursor,
                last_attempt_at=clock,
                last_success_at=attempt.last_success_at,
                error_count=attempt.error_count + 1,
                last_error="; ".join(batch.errors),
                indexed_count=attempt.indexed_count,
                failed_count=attempt.failed_count + 1,
            )
            self._states.upsert(failed)
            warning = freshness_warning(
                failed,
                max_age_seconds=self._freshness_max_age_seconds,
                now=clock,
            )
            return ConnectorSyncResult(
                connector=connector.name,
                project=project,
                ok=False,
                batch=batch,
                state=failed,
                stale=True,
                freshness_warning=warning,
                error=failed.last_error,
            )

        prior_revisions: dict[str, tuple[str, ...]] = {}
        if self._memory_review_service is not None:
            for record in batch.records:
                prior_revisions[record.source_id] = tuple(
                    item.revision
                    for item in self._sources.all(
                        tenant_id=project.tenant_id,
                        project_id=project.project_id,
                        include_revoked=True,
                    )
                    if item.source_id == record.source_id
                )
        applied = apply_sync_batch(
            self._index,
            batch,
            connector_name=connector.name,
            project=project,
            source_store=self._sources,
        )
        knowledge_indexed_count = 0
        knowledge_index_error = None
        if self._knowledge_index_sync is not None:
            try:
                knowledge_items = [
                    item
                    for item in (*batch.added, *batch.updated)
                    if item.project.tenant_id == project.tenant_id
                    and item.project.project_id == project.project_id
                ]
                if batch.deleted:
                    revoked_ids = set(batch.deleted)
                    knowledge_items.extend(
                        item
                        for item in self._index.all(include_revoked=True)
                        if item.source.source_id in revoked_ids
                        and item.project.tenant_id == project.tenant_id
                        and item.project.project_id == project.project_id
                    )
                knowledge_result = self._knowledge_index_sync.sync(
                    tuple(knowledge_items),
                    mode="incremental",
                )
                knowledge_indexed_count = knowledge_result.build.chunks_written
            except Exception as exc:  # noqa: BLE001 - optional index boundary
                knowledge_index_error = type(exc).__name__
        success = ConnectorSyncState(
            connector=connector.name,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            cursor=batch.next_cursor or attempt.cursor,
            last_attempt_at=clock,
            last_success_at=clock,
            error_count=0,
            last_error=None,
            indexed_count=attempt.indexed_count + applied.added_count + applied.updated_count,
            failed_count=attempt.failed_count,
        )
        self._states.upsert(success)
        if self._memory_review_service is not None:
            for record in batch.records:
                for old_revision in prior_revisions.get(record.source_id, ()):
                    if old_revision != record.revision:
                        self._memory_review_service.open_review_for_source_revision(
                            record.source_id,
                            old_revision,
                            record.revision,
                            project=project,
                        )
            for source_id in batch.deleted:
                self._memory_review_service.open_review_for_source_revoked(
                    source_id,
                    project=project,
                )
        warning = freshness_warning(
            success,
            max_age_seconds=self._freshness_max_age_seconds,
            now=clock,
        )
        return ConnectorSyncResult(
            connector=connector.name,
            project=project,
            ok=True,
            batch=batch,
            state=success,
            added_count=applied.added_count,
            updated_count=applied.updated_count,
            revoked_count=applied.revoked_count,
            stale=sync_state_is_stale(
                success,
                max_age_seconds=self._freshness_max_age_seconds,
                now=clock,
            ),
            freshness_warning=warning,
            knowledge_indexed_count=knowledge_indexed_count,
            knowledge_index_error=knowledge_index_error,
        )

    def freshness_for(
        self,
        *,
        connector: str,
        project: ProjectRef,
        now: datetime | None = None,
    ) -> str | None:
        state = self._states.get(connector, project.tenant_id, project.project_id)
        return freshness_warning(
            state,
            max_age_seconds=self._freshness_max_age_seconds,
            now=now,
        )


@dataclass(frozen=True)
class AppliedSyncBatch:
    added_count: int
    updated_count: int
    revoked_count: int


def apply_sync_batch(
    index: EvidenceIndex,
    batch: SyncBatch,
    *,
    connector_name: str,
    project: ProjectRef | None = None,
    source_store: SourceRecordStore | None = None,
) -> AppliedSyncBatch:
    """Persist immutable source revisions, index new ones, and revoke deletes."""

    source_store = source_store or InMemorySourceRecordStore()
    known = _known_source_ids(index, system=connector_name)
    added_items: list[Evidence] = []
    updated_items: list[Evidence] = []

    for item in (*batch.added, *batch.updated):
        if item.source.system != connector_name:
            # Keep source.system as authority; still index but count as added.
            pass
        record = _record_for(item, batch=batch)
        if project is not None and (
            record.tenant_id and record.tenant_id != project.tenant_id
            or record.project_id != project.project_id
        ):
            continue
        if not source_store.put(record):
            continue
        key = item.source.source_id
        if key in known:
            updated_items.append(item)
        else:
            added_items.append(item)
        known.add(key)

    if added_items or updated_items:
        index.add_many((*added_items, *updated_items))

    revoked = 0
    if batch.deleted:
        revoke = getattr(index, "revoke_source_ids", None)
        if callable(revoke):
            revoked = int(revoke(batch.deleted, at=batch.observed_at))
        if project is not None:
            for source_id in batch.deleted:
                source_store.revoke(project.tenant_id, project.project_id, source_id)

    return AppliedSyncBatch(
        added_count=len(added_items),
        updated_count=len(updated_items),
        revoked_count=revoked,
    )


def _record_for(item: Evidence, *, batch: SyncBatch) -> SourceRecord:
    for record in batch.records:
        if record.source_id == item.source.source_id and record.project_id == item.project.project_id:
            return record
    return SourceRecord.from_evidence(item)


def _known_source_ids(index: EvidenceIndex, *, system: str) -> set[str]:
    include_revoked = True
    try:
        items = index.all(include_revoked=include_revoked)
    except TypeError:
        items = index.all()
    return {
        item.source.source_id
        for item in items
        if item.source.system == system and not getattr(item, "revoked", False)
    }


__all__ = [
    "AppliedSyncBatch",
    "ConnectorSyncResult",
    "ConnectorSyncService",
    "SyncCursor",
    "apply_sync_batch",
]
