"""Validated embedding generation for knowledge chunks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from project_lens.context.knowledge_index.models import (
    EmbeddingRecord,
    KnowledgeChunk,
    KnowledgeIndexJob,
)
from project_lens.context.knowledge_index.store import SQLiteKnowledgeIndexStore


class EmbeddingGenerationError(RuntimeError):
    """A provider response cannot be safely persisted."""


@dataclass(frozen=True)
class EmbeddingBatchResult:
    completed: int
    failed: int
    cache_hits: int
    errors: tuple[str, ...] = ()


class KnowledgeEmbeddingService:
    def __init__(
        self,
        store: SQLiteKnowledgeIndexStore,
        provider,
        *,
        model_version: str | None = None,
        index_version: str = "v1",
        max_batch_size: int = 32,
    ) -> None:
        self._store = store
        self._provider = provider
        self._model_version = model_version or str(provider.model_version)
        self._index_version = index_version
        self._max_batch_size = max(1, min(int(max_batch_size), 128))

    @property
    def model_version(self) -> str:
        return self._model_version

    def enqueue_missing_embeddings(
        self,
        chunks: Sequence[KnowledgeChunk],
        *,
        status: str = "pending",
    ) -> tuple[KnowledgeIndexJob, ...]:
        jobs: list[KnowledgeIndexJob] = []
        for chunk in chunks:
            if not chunk.indexable:
                continue
            cached = self._store.get_embedding(
                tenant_id=chunk.tenant_id,
                project_id=chunk.project_id,
                chunk_id=chunk.chunk_id,
                model_version=self._model_version,
                content_hash=chunk.content_hash,
                chunker_version=chunk.chunker_version,
                index_version=self._index_version,
            )
            if cached is not None and cached.status == "ready":
                continue
            jobs.append(
                self._store.enqueue_job(
                    KnowledgeIndexJob(
                        tenant_id=chunk.tenant_id,
                        project_id=chunk.project_id,
                        object_kind=chunk.object_kind,
                        object_id=chunk.object_id,
                        content_hash=chunk.content_hash,
                        model_version=self._model_version,
                        chunker_version=chunk.chunker_version,
                        status=status,
                    )
                )
            )
        return tuple(jobs)

    def embed_chunks(self, chunks: Sequence[KnowledgeChunk]) -> EmbeddingBatchResult:
        pending = [
            chunk
            for chunk in chunks
            if chunk.indexable
            and self._store.get_embedding(
                tenant_id=chunk.tenant_id,
                project_id=chunk.project_id,
                chunk_id=chunk.chunk_id,
                model_version=self._model_version,
                content_hash=chunk.content_hash,
                chunker_version=chunk.chunker_version,
                index_version=self._index_version,
            )
            is None
        ]
        cache_hits = len(chunks) - len(pending)
        completed = 0
        failed = 0
        errors: list[str] = []
        for start in range(0, len(pending), self._max_batch_size):
            batch = pending[start : start + self._max_batch_size]
            try:
                vectors = self._embed_batch(batch)
                for chunk, vector in zip(batch, vectors, strict=True):
                    self._store.put_embedding(
                        EmbeddingRecord(
                            chunk_id=chunk.chunk_id,
                            tenant_id=chunk.tenant_id,
                            project_id=chunk.project_id,
                            model_version=self._model_version,
                            dimensions=len(vector),
                            content_hash=chunk.content_hash,
                            chunker_version=chunk.chunker_version,
                            index_version=self._index_version,
                            vector=vector,
                        )
                    )
                completed += len(batch)
            except Exception as exc:
                failed += len(batch)
                errors.append(str(exc))
        return EmbeddingBatchResult(completed, failed, cache_hits, tuple(errors))

    def process_pending(
        self,
        chunks: Sequence[KnowledgeChunk],
        *,
        max_attempts: int = 3,
    ) -> EmbeddingBatchResult:
        jobs = self.enqueue_missing_embeddings(chunks)
        result = self.embed_chunks(chunks)
        for job in jobs:
            status = "completed" if result.failed == 0 else "failed"
            updated = job.model_copy(
                update={
                    "status": status,
                    "attempt_count": job.attempt_count + 1,
                    "last_error": result.errors[0] if result.errors else None,
                }
            )
            if updated.attempt_count >= max_attempts and status == "failed":
                updated = updated.model_copy(update={"status": "dead_letter"})
            self._store.enqueue_job(updated)
        return result

    def _embed_batch(self, chunks: Sequence[KnowledgeChunk]) -> tuple[tuple[float, ...], ...]:
        vectors = self._provider.embed([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise EmbeddingGenerationError("embedding provider returned an invalid vector count")
        normalized: list[tuple[float, ...]] = []
        dimensions: int | None = None
        for vector in vectors:
            if not vector:
                raise EmbeddingGenerationError("embedding provider returned an empty vector")
            values = tuple(float(value) for value in vector)
            dimensions = dimensions or len(values)
            if len(values) != dimensions:
                raise EmbeddingGenerationError("embedding provider returned inconsistent dimensions")
            normalized.append(values)
        return tuple(normalized)


__all__ = ["EmbeddingBatchResult", "EmbeddingGenerationError", "KnowledgeEmbeddingService"]
