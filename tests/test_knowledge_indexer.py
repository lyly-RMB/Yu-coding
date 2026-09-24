from __future__ import annotations

from datetime import datetime, timezone

from project_lens.context.knowledge_index import (
    KnowledgeEmbeddingService,
    KnowledgeIndexBuildService,
    KnowledgeIndexVectorRetriever,
    SQLiteKnowledgeIndexStore,
)
from project_lens.context.knowledge_index.models import KnowledgeChunk
from project_lens.domain.models import Evidence, EvidenceType, ProjectRef, SourceRef
from project_lens.persistence.sqlite import SQLiteDatabase


class FakeProvider:
    model_version = "fake-v1"

    def embed(self, texts):
        return tuple((1.0, 0.0) for _ in texts)


def _evidence(text: str, *, revoked: bool = False) -> Evidence:
    return Evidence(
        type=EvidenceType.DOCUMENT,
        project=ProjectRef(tenant_id="t1", project_id="p1"),
        source=SourceRef(system="docs", source_id="doc-1"),
        content=text,
        observed_at=datetime.now(timezone.utc),
        access_scope="project:p1:read",
        content_hash=("source-" + text).encode().hex()[:64].ljust(64, "0"),
        revoked=revoked,
    )


def test_indexer_builds_chunks_and_persistent_embeddings() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    provider = FakeProvider()
    service = KnowledgeIndexBuildService(
        store,
        embedding_service=KnowledgeEmbeddingService(store, provider),
    )

    result = service.index_evidence(_evidence("支付回调必须幂等。"))

    assert result.documents_upserted == 1
    assert result.chunks_written == 1
    assert result.embeddings is not None
    assert result.embeddings.completed == 1
    assert len(
        store.list_chunks_with_embeddings(
            tenant_id="t1",
            project_id="p1",
            model_version="fake-v1",
        )
    ) == 1


def test_indexer_replaces_changed_content_and_deactivates_old_chunks() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    service = KnowledgeIndexBuildService(
        store,
        embedding_service=KnowledgeEmbeddingService(store, FakeProvider()),
    )

    first = service.index_evidence(_evidence("第一版内容。"))
    second = service.index_evidence(_evidence("第二版内容。"))

    assert first.chunks_written == 1
    assert second.chunks_written == 1
    assert second.chunks_deactivated == 1
    chunks = store.list_chunks(tenant_id="t1", project_id="p1")
    assert len(chunks) == 2
    assert sum(item.indexable for item in chunks) == 1
    assert sum(item.status == "superseded" for item in chunks) == 1
    assert len(
        store.list_chunks_with_embeddings(
            tenant_id="t1",
            project_id="p1",
            model_version="fake-v1",
        )
    ) == 1


def test_indexer_revocation_deactivates_current_chunks() -> None:
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    service = KnowledgeIndexBuildService(
        store,
        embedding_service=KnowledgeEmbeddingService(store, FakeProvider()),
    )

    service.index_evidence(_evidence("有效内容。"))
    result = service.index_evidence(_evidence("有效内容。", revoked=True))

    assert result.chunks_written == 0
    assert result.chunks_deactivated == 1
    assert store.list_chunks(
        tenant_id="t1",
        project_id="p1",
        indexable_only=True,
    ) == ()
