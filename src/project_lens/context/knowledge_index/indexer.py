"""Full and incremental construction of the durable knowledge index."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from project_lens.context.knowledge_index.adapters import EvidenceAdapter
from project_lens.context.knowledge_index.chunking import KnowledgeChunkBuilder
from project_lens.context.knowledge_index.embeddings import (
    EmbeddingBatchResult,
    KnowledgeEmbeddingService,
)
from project_lens.context.knowledge_index.models import KnowledgeChunk
from project_lens.context.knowledge_index.store import SQLiteKnowledgeIndexStore
from project_lens.domain.models import Evidence


@dataclass(frozen=True)
class KnowledgeIndexBuildResult:
    documents_upserted: int
    chunks_written: int
    chunks_deactivated: int
    embeddings: EmbeddingBatchResult | None


class KnowledgeIndexBuildService:
    """Project-scoped Evidence -> document -> chunk -> embedding pipeline."""

    def __init__(
        self,
        store: SQLiteKnowledgeIndexStore,
        *,
        embedding_service: KnowledgeEmbeddingService | None = None,
        adapter: EvidenceAdapter | None = None,
        chunk_builder: KnowledgeChunkBuilder | None = None,
    ) -> None:
        self._store = store
        self._embedding = embedding_service
        self._adapter = adapter or EvidenceAdapter()
        self._chunk_builder = chunk_builder or KnowledgeChunkBuilder()

    def index_evidence(self, evidence: Evidence) -> KnowledgeIndexBuildResult:
        document = self._adapter.adapt(evidence)
        chunks = self._chunk_builder.build(document)
        previous = self._store.list_chunks(
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            indexable_only=True,
            limit=50_000,
        )
        previous_for_document = {
            chunk.chunk_id
            for chunk in previous
            if chunk.document_id == document.id
        }
        prior_documents = self._store.list_documents(
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            kind=document.kind,
            indexable_only=True,
            limit=50_000,
        )
        superseded_document_ids = {
            prior.id
            for prior in prior_documents
            if prior.id != document.id
            and prior.source_system == document.source_system
            and prior.source_id == document.source_id
        }
        self._store.upsert_documents((document,))
        deactivated = 0
        for document_id in superseded_document_ids:
            deactivated += self._store.deactivate_document_chunks(document_id)
            self._store.deactivate_document(document_id)
        self._store.replace_document_chunks(document.id, chunks)
        embedding_result = (
            self._embedding.process_pending(chunks)
            if self._embedding is not None and chunks
            else None
        )
        return KnowledgeIndexBuildResult(
            documents_upserted=1,
            chunks_written=len(chunks),
            chunks_deactivated=deactivated
            + len(previous_for_document - {c.chunk_id for c in chunks}),
            embeddings=embedding_result,
        )

    def index_many(self, evidence: Iterable[Evidence]) -> KnowledgeIndexBuildResult:
        total = KnowledgeIndexBuildResult(0, 0, 0, None)
        for item in evidence:
            result = self.index_evidence(item)
            total = KnowledgeIndexBuildResult(
                documents_upserted=total.documents_upserted + result.documents_upserted,
                chunks_written=total.chunks_written + result.chunks_written,
                chunks_deactivated=total.chunks_deactivated + result.chunks_deactivated,
                embeddings=_merge_embedding_results(total.embeddings, result.embeddings),
            )
        return total


def _merge_embedding_results(
    left: EmbeddingBatchResult | None,
    right: EmbeddingBatchResult | None,
) -> EmbeddingBatchResult | None:
    if left is None:
        return right
    if right is None:
        return left
    return EmbeddingBatchResult(
        completed=left.completed + right.completed,
        failed=left.failed + right.failed,
        cache_hits=left.cache_hits + right.cache_hits,
        errors=left.errors + right.errors,
    )


__all__ = ["KnowledgeIndexBuildResult", "KnowledgeIndexBuildService", "_merge_embedding_results"]
