"""Vector retrieval backed by the durable unified knowledge index."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timezone

from project_lens.context.knowledge_index.store import SQLiteKnowledgeIndexStore
from project_lens.context.retrieval.types import ChannelHit
from project_lens.domain.models import Evidence


class KnowledgeIndexVectorRetriever:
    """Query persisted chunk vectors after scope and lifecycle filtering."""

    def __init__(
        self,
        store: SQLiteKnowledgeIndexStore,
        provider,
        *,
        model_version: str | None = None,
        chunker_version: str | None = None,
        index_version: str = "v1",
        evidence_lookup=None,
        now=None,
        fallback_retriever=None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._model_version = model_version or str(provider.model_version)
        self._chunker_version = chunker_version
        self._index_version = index_version
        self._evidence_lookup = evidence_lookup
        self._now = now
        self._fallback_retriever = fallback_retriever
        self._last_stats: dict[str, float | int | None] = {}

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def last_stats(self) -> dict[str, float | int | None]:
        return dict(self._last_stats)

    def retrieve(
        self,
        query: str,
        candidates: Sequence[Evidence],
        *,
        limit: int,
    ) -> list[ChannelHit]:
        if not query.strip() or not candidates:
            return []
        project = candidates[0].project
        candidate_ids = {str(item.id): item for item in candidates}
        rows = self._store.list_chunks_with_embeddings(
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            model_version=self._model_version,
            chunker_version=self._chunker_version,
            index_version=self._index_version,
        )
        eligible: list[tuple[object, object, Evidence]] = []
        persisted_evidence_ids: set[str] = set()
        persisted_source_keys: set[tuple[str, str]] = set()
        skipped_lifecycle = 0
        for chunk, record in rows:
            source_system = str(chunk.metadata.get("source_system") or "")
            if source_system and chunk.source_id:
                persisted_source_keys.add((source_system, chunk.source_id))
            evidence = self._resolve_evidence(chunk, candidate_ids)
            if evidence is None:
                continue
            persisted_evidence_ids.add(str(evidence.id))
            persisted_source_keys.add((evidence.source.system, evidence.source.source_id))
            if not _is_current(chunk, self._now):
                skipped_lifecycle += 1
                continue
            eligible.append((chunk, record, evidence))
        self._last_stats = {
            "persisted_vector_rows": len(rows),
            "vector_eligible_rows": len(eligible),
            "vector_lifecycle_filtered": skipped_lifecycle,
            "embedding_cache_lookups": 1,
            "embedding_cache_hits": 0,
            "embedding_cache_hit_rate": 0.0,
        }
        best_by_evidence: dict[str, ChannelHit] = {}
        if eligible:
            query_vectors = self._provider.embed([query[:2_000]])
            if len(query_vectors) != 1:
                raise RuntimeError("embedding provider returned an invalid query vector count")
            query_vector = _valid_vector(query_vectors[0])
            for chunk, record, evidence in eligible:
                score = _cosine(query_vector, record.vector)
                current = best_by_evidence.get(str(evidence.id))
                hit = ChannelHit(evidence=evidence, score=score)
                if current is None or hit.score > current.score:
                    best_by_evidence[str(evidence.id)] = hit
        if self._fallback_retriever is not None:
            missing_candidates = [
                item
                for item in candidates
                if str(item.id) not in persisted_evidence_ids
                and (item.source.system, item.source.source_id) not in persisted_source_keys
            ]
            if missing_candidates:
                for hit in self._fallback_retriever.retrieve(
                    query,
                    missing_candidates,
                    limit=limit,
                ):
                    current = best_by_evidence.get(str(hit.evidence.id))
                    if current is None or hit.score > current.score:
                        best_by_evidence[str(hit.evidence.id)] = hit
                self._last_stats["vector_fallback_candidate_count"] = len(missing_candidates)
            else:
                self._last_stats["vector_fallback_candidate_count"] = 0
        return sorted(
            best_by_evidence.values(),
            key=lambda hit: (-hit.score, hit.evidence.source.source_id),
        )[: max(1, int(limit))]

    def _resolve_evidence(self, chunk, candidates):
        for evidence_id in chunk.metadata.get("evidence_ids", ()):
            if str(evidence_id) in candidates:
                return candidates[str(evidence_id)]
        if chunk.object_kind.value == "evidence" and chunk.object_id in candidates:
            return candidates[chunk.object_id]
        if self._evidence_lookup is not None:
            return self._evidence_lookup(chunk)
        return None


def _is_current(chunk, now=None) -> bool:
    if str(chunk.status).casefold() != "active":
        return False
    current = now or datetime.now(timezone.utc)
    valid_from = _metadata_datetime(chunk, "valid_from")
    valid_to = _metadata_datetime(chunk, "valid_to")
    if valid_from is not None and current < valid_from:
        return False
    if valid_to is not None and current >= valid_to:
        return False
    return True


def _metadata_datetime(chunk, key):
    value = chunk.metadata.get(key)
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _valid_vector(vector) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    if not values:
        raise RuntimeError("embedding provider returned an empty vector")
    return values


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0.0
    return max(
        0.0,
        min(
            1.0,
            sum(a * b for a, b in zip(left, right, strict=True)) / denominator,
        ),
    )


__all__ = ["KnowledgeIndexVectorRetriever"]
