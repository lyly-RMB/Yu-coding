"""Sync Feishu docs into Evidence via whitelist tokens (not message hot path)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from project_lens.application.feishu_doc_sync_status import FeishuDocSyncStatusStore
from project_lens.context.indexing.feishu_documents import FeishuDocumentIndexer
from project_lens.context.sources.feishu_docs import to_feishu_document_record, to_source_record
from project_lens.context.source_store import SourceRecordStore
from project_lens.context.store import InMemoryEvidenceIndex
from project_lens.domain.feishu_doc_sync import FeishuDocSyncStatus, FeishuDocSyncStatusValue
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.docs_client import FeishuDocClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeishuDocSyncResult:
    project_id: str
    requested: int
    fetched: int
    indexed: int
    skipped_unchanged: int
    failed: tuple[str, ...] = ()
    statuses: tuple[FeishuDocSyncStatus, ...] = ()


class FeishuDocumentSyncService:
    """Pull whitelist docs through OpenAPI and upsert into the evidence index."""

    def __init__(
        self,
        *,
        client: FeishuDocClient | None,
        index: InMemoryEvidenceIndex,
        status_store: FeishuDocSyncStatusStore | None = None,
        indexer: FeishuDocumentIndexer | None = None,
        source_store: SourceRecordStore | None = None,
        knowledge_gap_service=None,
    ) -> None:
        self._client = client
        self._index = index
        self._status_store = status_store
        self._indexer = indexer or FeishuDocumentIndexer()
        self._source_store = source_store
        self._knowledge_gap_service = knowledge_gap_service

    @property
    def available(self) -> bool:
        return self._client is not None

    def sync_tokens(
        self,
        *,
        project: ProjectRef,
        access_scope: str,
        doc_tokens: tuple[str, ...],
    ) -> FeishuDocSyncResult:
        if self._client is None:
            raise RuntimeError("Feishu document API client is not configured")
        if not doc_tokens:
            return FeishuDocSyncResult(
                project_id=project.project_id,
                requested=0,
                fetched=0,
                indexed=0,
                skipped_unchanged=0,
            )

        existing_revisions = _known_revisions(self._index, self._status_store, project)
        fetched = 0
        indexed = 0
        skipped = 0
        failed: list[str] = []
        statuses: list[FeishuDocSyncStatus] = []
        now = datetime.now(timezone.utc)

        for token in doc_tokens:
            previous = (
                self._status_store.get(project, token) if self._status_store is not None else None
            )
            try:
                raw = self._client.get_document(token)
                fetched += 1
            except Exception as exc:  # noqa: BLE001 - sync must continue other tokens
                logger.warning("feishu doc sync failed token=%s error=%s", token, exc)
                failed.append(f"{token}: {exc}")
                status = _build_status(
                    project=project,
                    doc_token=token,
                    now=now,
                    status=FeishuDocSyncStatusValue.FAILED,
                    access_scope=access_scope,
                    previous=previous,
                    error=str(exc)[:2_000],
                )
                statuses.append(_persist_status(self._status_store, status))
                continue

            if existing_revisions.get(token) == raw.revision:
                skipped += 1
                status = _build_status(
                    project=project,
                    doc_token=token,
                    now=now,
                    status=FeishuDocSyncStatusValue.SKIPPED,
                    access_scope=access_scope,
                    previous=previous,
                    raw=raw,
                )
                statuses.append(_persist_status(self._status_store, status))
                continue

            try:
                record = to_feishu_document_record(
                    raw,
                    project=project,
                    access_scope=access_scope,
                )
                evidence = self._indexer.index([record], project=project)
            except ValueError as exc:
                failed.append(f"{token}: {exc}")
                status = _build_status(
                    project=project,
                    doc_token=token,
                    now=now,
                    status=FeishuDocSyncStatusValue.FAILED,
                    access_scope=access_scope,
                    previous=previous,
                    raw=raw,
                    error=str(exc)[:2_000],
                    keep_previous_revision=True,
                )
                statuses.append(_persist_status(self._status_store, status))
                continue

            if self._source_store is None:
                # Preserve the legacy in-memory service contract for isolated callers.
                self._index.remove_source_prefix(
                    system="feishu_doc",
                    source_id_prefix=f"{token}#",
                )
            else:
                self._source_store.put(to_source_record(record))
            indexed += self._index.add_many(evidence)
            if self._knowledge_gap_service is not None:
                try:
                    self._knowledge_gap_service.enqueue_source_rechecks(
                        project=project,
                        source_ref=f"feishu_doc:{token}",
                        revision=str(raw.revision),
                        now=now,
                    )
                except Exception:  # noqa: BLE001 - indexing success must not be undone by queue failure
                    logger.exception(
                        "knowledge-gap recheck enqueue failed project=%s",
                        project.project_id,
                    )
            status = _build_status(
                project=project,
                doc_token=token,
                now=now,
                status=FeishuDocSyncStatusValue.SUCCESS,
                access_scope=access_scope,
                previous=previous,
                raw=raw,
            )
            statuses.append(_persist_status(self._status_store, status))

        return FeishuDocSyncResult(
            project_id=project.project_id,
            requested=len(doc_tokens),
            fetched=fetched,
            indexed=indexed,
            skipped_unchanged=skipped,
            failed=tuple(failed),
            statuses=tuple(statuses),
        )


def _build_status(
    *,
    project: ProjectRef,
    doc_token: str,
    now: datetime,
    status: FeishuDocSyncStatusValue,
    access_scope: str,
    previous: FeishuDocSyncStatus | None = None,
    raw: object | None = None,
    error: str | None = None,
    keep_previous_revision: bool = False,
) -> FeishuDocSyncStatus:
    """Compose sync status while preserving url / scope / last success across failures."""

    raw_title = getattr(raw, "title", None) if raw is not None else None
    raw_owner = getattr(raw, "owner_user_id", None) if raw is not None else None
    raw_url = getattr(raw, "doc_url", None) if raw is not None else None
    raw_revision = getattr(raw, "revision", None) if raw is not None else None

    if status in {FeishuDocSyncStatusValue.SUCCESS, FeishuDocSyncStatusValue.SKIPPED}:
        revision = str(raw_revision) if raw_revision is not None else None
        last_success = revision
    else:
        revision = previous.revision if previous is not None else None
        if keep_previous_revision is False and previous is None:
            revision = None
        last_success = _carry_last_success_revision(previous)

    return FeishuDocSyncStatus(
        project=project,
        doc_token=doc_token,
        revision=revision,
        last_synced_at=now,
        status=status,
        error=error,
        title=(raw_title or None) or (previous.title if previous else None),
        owner_user_id=(raw_owner or None)
        or (previous.owner_user_id if previous else None),
        doc_url=(raw_url or None) or (previous.doc_url if previous else None),
        access_scope=access_scope or (previous.access_scope if previous else None),
        last_success_revision=last_success,
    )


def _carry_last_success_revision(previous: FeishuDocSyncStatus | None) -> str | None:
    if previous is None:
        return None
    if previous.last_success_revision:
        return previous.last_success_revision
    if previous.status in {
        FeishuDocSyncStatusValue.SUCCESS,
        FeishuDocSyncStatusValue.SKIPPED,
    }:
        return previous.revision
    return None


def _persist_status(
    store: FeishuDocSyncStatusStore | None,
    status: FeishuDocSyncStatus,
) -> FeishuDocSyncStatus:
    if store is None:
        return status
    try:
        return store.upsert(status)
    except Exception:  # noqa: BLE001 - status persistence must not abort sync
        logger.exception(
            "feishu doc sync status persist failed token=%s",
            status.doc_token,
        )
        return status


def _known_revisions(
    index: InMemoryEvidenceIndex,
    status_store: FeishuDocSyncStatusStore | None,
    project: ProjectRef,
) -> dict[str, str]:
    revisions = _indexed_revisions(index)
    if status_store is None:
        return revisions
    try:
        for item in status_store.list_for_project(project):
            if item.revision and item.status != FeishuDocSyncStatusValue.FAILED:
                revisions.setdefault(item.doc_token, item.revision)
    except Exception:  # noqa: BLE001 - status read must not abort sync
        logger.exception("feishu doc sync status read failed project=%s", project.project_id)
    return revisions


def _indexed_revisions(index: InMemoryEvidenceIndex) -> dict[str, str]:
    revisions: dict[str, str] = {}
    for item in index.all():
        if item.source.system != "feishu_doc":
            continue
        token = item.metadata.get("doc_token")
        revision = item.metadata.get("revision")
        if token and revision:
            revisions[str(token)] = str(revision)
    return revisions
