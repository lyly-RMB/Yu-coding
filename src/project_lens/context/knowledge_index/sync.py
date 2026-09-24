"""Explicit, bounded synchronization for the durable knowledge index."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from project_lens.context.knowledge_index.indexer import (
    KnowledgeIndexBuildResult,
    KnowledgeIndexBuildService,
)
from project_lens.context.knowledge_index.models import KnowledgeObjectKind
from project_lens.context.knowledge_index.store import (
    KnowledgeIndexStats,
    SQLiteKnowledgeIndexStore,
)
from project_lens.domain.models import Evidence
from project_lens.domain.models import ProjectRef


@dataclass(frozen=True)
class KnowledgeIndexSyncResult:
    mode: str
    dry_run: bool
    batches: int
    input_records: int
    deactivated_documents: int
    build: KnowledgeIndexBuildResult
    stats: KnowledgeIndexStats


class KnowledgeIndexSyncService:
    """Run explicit incremental/full syncs without doing work at app startup."""

    def __init__(
        self,
        store: SQLiteKnowledgeIndexStore,
        builder: KnowledgeIndexBuildService,
        *,
        batch_size: int = 100,
    ) -> None:
        self._store = store
        self._builder = builder
        self._batch_size = max(1, min(int(batch_size), 1_000))

    def sync(
        self,
        evidence: Iterable[Evidence],
        *,
        mode: str = "incremental",
        dry_run: bool = False,
        project: ProjectRef | None = None,
    ) -> KnowledgeIndexSyncResult:
        if mode not in {"incremental", "full"}:
            raise ValueError("knowledge index sync mode must be incremental or full")
        items = tuple(evidence)
        batches = _batches(items, self._batch_size)
        build = _empty_build()
        deactivated = 0
        if dry_run:
            for batch in batches:
                build = _merge_build(build, _preview(self._builder, batch))
        else:
            for batch in batches:
                build = _merge_build(build, self._builder.index_many(batch))
            if mode == "full":
                present = {
                    (item.source.system, item.source.source_id)
                    for item in items
                }
                for document in self._store.list_documents(
                    tenant_id=project.tenant_id if project else (items[0].project.tenant_id if items else ""),
                    project_id=project.project_id if project else (items[0].project.project_id if items else ""),
                    kind=KnowledgeObjectKind.EVIDENCE,
                    indexable_only=True,
                    limit=50_000,
                ) if project or items else ():
                    if (document.source_system, document.source_id) not in present:
                        deactivated += self._store.deactivate_document(document.id)
        scope = _scope(items, project=project)
        return KnowledgeIndexSyncResult(
            mode=mode,
            dry_run=dry_run,
            batches=len(batches),
            input_records=len(items),
            deactivated_documents=deactivated,
            build=build,
            stats=self._store.stats(**scope),
        )


def _preview(builder, batch):
    from project_lens.context.knowledge_index.adapters import EvidenceAdapter

    documents = [EvidenceAdapter().adapt(item) for item in batch]
    chunks = [builder._chunk_builder.build(document) for document in documents]
    return KnowledgeIndexBuildResult(
        documents_upserted=len(documents),
        chunks_written=sum(len(item) for item in chunks),
        chunks_deactivated=0,
        embeddings=None,
    )


def _batches(items, size):
    return tuple(items[start : start + size] for start in range(0, len(items), size))


def _scope(items, *, project=None):
    if project is not None:
        return {
            "tenant_id": project.tenant_id,
            "project_id": project.project_id,
        }
    if not items:
        return {}
    return {
        "tenant_id": items[0].project.tenant_id,
        "project_id": items[0].project.project_id,
    }


def _empty_build():
    return KnowledgeIndexBuildResult(0, 0, 0, None)


def _merge_build(left, right):
    from project_lens.context.knowledge_index.indexer import _merge_embedding_results

    return KnowledgeIndexBuildResult(
        documents_upserted=left.documents_upserted + right.documents_upserted,
        chunks_written=left.chunks_written + right.chunks_written,
        chunks_deactivated=left.chunks_deactivated + right.chunks_deactivated,
        embeddings=_merge_embedding_results(left.embeddings, right.embeddings),
    )


__all__ = ["KnowledgeIndexSyncResult", "KnowledgeIndexSyncService"]
