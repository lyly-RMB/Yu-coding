from __future__ import annotations

from datetime import datetime, timedelta, timezone

from project_lens.context.knowledge_index import (
    EvidenceAdapter,
    KnowledgeChunkBuilder,
    KnowledgeEmbeddingService,
    KnowledgeIndexVectorRetriever,
    SQLiteKnowledgeIndexStore,
)
from project_lens.context.retrieval.vector import EvidenceVectorRetriever
from project_lens.domain.models import Evidence, EvidenceType, ProjectRef, SourceRef
from project_lens.persistence.sqlite import SQLiteDatabase


class QueryAwareProvider:
    model_version = "fake-v1"

    def embed(self, texts):
        return tuple(
            (1.0, 0.0)
            if "支付" in text or "payment" in text.casefold()
            else (0.0, 1.0)
            for text in texts
        )


def _evidence(
    source_id: str,
    text: str,
    *,
    scope: str = "project:p1:read",
    metadata: dict | None = None,
) -> Evidence:
    return Evidence(
        type=EvidenceType.DOCUMENT,
        project=ProjectRef(tenant_id="t1", project_id="p1"),
        source=SourceRef(system="docs", source_id=source_id),
        content=text,
        observed_at=datetime.now(timezone.utc),
        access_scope=scope,
        content_hash=(source_id.encode().hex() + "0" * 64)[:64],
        metadata=metadata or {},
    )


def _index(store, evidence: Evidence):
    document = EvidenceAdapter().adapt(evidence)
    chunks = KnowledgeChunkBuilder().build(document)
    store.upsert_documents((document,))
    store.upsert_chunks(chunks)
    KnowledgeEmbeddingService(store, QueryAwareProvider()).embed_chunks(chunks)
    return chunks


def test_persisted_vector_query_returns_provenance_back_to_authorized_evidence() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    allowed = _evidence("allowed", "支付回调必须幂等。")
    _index(store, allowed)

    retriever = KnowledgeIndexVectorRetriever(store, QueryAwareProvider())
    hits = retriever.retrieve("payment callback", (allowed,), limit=5)

    assert [hit.evidence.source.source_id for hit in hits] == ["allowed"]
    assert retriever.last_stats["persisted_vector_rows"] == 1
    assert retriever.last_stats["vector_eligible_rows"] == 1


def test_persisted_vector_query_falls_back_for_unindexed_candidates() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    allowed = _evidence("allowed", "支付回调必须幂等。")
    provider = QueryAwareProvider()
    retriever = KnowledgeIndexVectorRetriever(
        store,
        provider,
        fallback_retriever=EvidenceVectorRetriever(provider),
    )

    hits = retriever.retrieve("payment callback", (allowed,), limit=5)

    assert [hit.evidence.source.source_id for hit in hits] == ["allowed"]
    assert retriever.last_stats["persisted_vector_rows"] == 0
    assert retriever.last_stats["vector_fallback_candidate_count"] == 1


def test_persisted_vector_query_does_not_fallback_to_superseded_source() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    current = _evidence("doc", "支付回调当前版本。")
    old = _evidence("doc", "支付回调旧版本。")
    _index(store, current)
    provider = QueryAwareProvider()
    retriever = KnowledgeIndexVectorRetriever(
        store,
        provider,
        fallback_retriever=EvidenceVectorRetriever(provider),
    )

    assert retriever.retrieve("payment callback", (old,), limit=5) == []
    assert retriever.last_stats["vector_fallback_candidate_count"] == 0


def test_persisted_vector_query_isolated_by_index_version() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    evidence = _evidence("versioned", "支付回调当前版本。")
    document = EvidenceAdapter().adapt(evidence)
    chunks = KnowledgeChunkBuilder().build(document)
    store.upsert_documents((document,))
    store.upsert_chunks(chunks)
    KnowledgeEmbeddingService(store, QueryAwareProvider(), index_version="v1").embed_chunks(chunks)
    KnowledgeEmbeddingService(store, QueryAwareProvider(), index_version="v2").embed_chunks(chunks)

    v1 = KnowledgeIndexVectorRetriever(store, QueryAwareProvider(), index_version="v1")
    v2 = KnowledgeIndexVectorRetriever(store, QueryAwareProvider(), index_version="v2")

    assert len(v1.retrieve("payment callback", (evidence,), limit=5)) == 1
    assert len(v2.retrieve("payment callback", (evidence,), limit=5)) == 1
    assert len(store.list_chunks_with_embeddings(
        tenant_id="t1", project_id="p1", model_version="fake-v1", index_version="v2"
    )) == 1


def test_persisted_vector_query_cannot_return_other_tenant_or_unlisted_candidate() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    allowed = _evidence("allowed", "普通文档。")
    secret = _evidence("secret", "支付回调必须幂等。")
    _index(store, allowed)
    _index(store, secret)

    retriever = KnowledgeIndexVectorRetriever(store, QueryAwareProvider())
    hits = retriever.retrieve("payment callback", (allowed,), limit=5)

    assert [hit.evidence.source.source_id for hit in hits] == ["allowed"]


def test_persisted_vector_query_filters_inactive_chunks_and_old_model() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    revoked = _evidence("revoked", "支付回调", metadata={"status": "revoked"})
    active = _evidence("active", "支付回调")
    revoked_chunks = _index(store, revoked)
    active_chunks = _index(store, active)
    assert revoked_chunks == ()

    retriever = KnowledgeIndexVectorRetriever(
        store,
        QueryAwareProvider(),
        model_version="missing-model",
    )
    assert retriever.retrieve("payment callback", (active,), limit=5) == []

    retriever = KnowledgeIndexVectorRetriever(store, QueryAwareProvider())
    assert [hit.evidence.source.source_id for hit in retriever.retrieve(
        "payment callback", (active,), limit=5
    )] == ["active"]
    assert active_chunks


def test_persisted_vector_query_respects_chunker_version() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    active = _evidence("active", "支付回调")
    chunks = _index(store, active)

    retriever = KnowledgeIndexVectorRetriever(
        store,
        QueryAwareProvider(),
        chunker_version="v2",
    )

    assert retriever.retrieve("payment callback", (active,), limit=5) == []
    assert chunks[0].chunker_version == "v1"


def test_persisted_vector_query_filters_expired_chunk_metadata() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    expired = _evidence(
        "expired",
        "支付回调",
        metadata={"valid_to": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
    )
    chunks = _index(store, expired)
    retriever = KnowledgeIndexVectorRetriever(store, QueryAwareProvider())

    assert retriever.retrieve("payment callback", (expired,), limit=5) == []
    assert retriever.last_stats["vector_lifecycle_filtered"] == len(chunks)
