from __future__ import annotations

from datetime import datetime, timezone

from project_lens.context.knowledge_index import (
    KnowledgeEmbeddingService,
    KnowledgeIndexBuildService,
    KnowledgeIndexSyncService,
    SQLiteKnowledgeIndexStore,
)
from project_lens.domain.models import Evidence, EvidenceType, ProjectRef, SourceRef
from project_lens.persistence.sqlite import SQLiteDatabase


class FakeProvider:
    model_version = "fake-v1"

    def embed(self, texts):
        return tuple((1.0, 0.0) for _ in texts)


def _evidence(source_id: str, text: str) -> Evidence:
    return Evidence(
        type=EvidenceType.DOCUMENT,
        project=ProjectRef(tenant_id="t1", project_id="p1"),
        source=SourceRef(system="docs", source_id=source_id),
        content=text,
        observed_at=datetime.now(timezone.utc),
        access_scope="project:p1:read",
        content_hash=(source_id + text).encode().hex()[:64].ljust(64, "0"),
    )


def _service():
    store = SQLiteKnowledgeIndexStore(SQLiteDatabase(":memory:"))
    builder = KnowledgeIndexBuildService(
        store,
        embedding_service=KnowledgeEmbeddingService(store, FakeProvider()),
    )
    return store, KnowledgeIndexSyncService(store, builder, batch_size=1)


def test_incremental_sync_is_batched_and_reports_index_stats() -> None:
    store, service = _service()
    result = service.sync(
        (
            _evidence("a", "支付回调"),
            _evidence("b", "订单状态"),
        ),
        mode="incremental",
    )

    assert result.batches == 2
    assert result.input_records == 2
    assert result.build.embeddings is not None
    assert result.build.embeddings.completed == 2
    assert result.stats.active_documents == 2
    assert result.stats.ready_embeddings == 2
    assert store.stats(tenant_id="t1", project_id="p1").active_chunks == 2


def test_dry_run_does_not_write_documents_or_embeddings() -> None:
    store, service = _service()
    result = service.sync(
        (_evidence("a", "支付回调"),),
        mode="incremental",
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.build.documents_upserted == 1
    assert result.stats.documents == 0
    assert store.stats().documents == 0


def test_full_sync_deactivates_source_missing_from_snapshot() -> None:
    store, service = _service()
    service.sync(
        (
            _evidence("a", "支付回调"),
            _evidence("b", "订单状态"),
        )
    )
    result = service.sync(
        (_evidence("a", "支付回调"),),
        mode="full",
    )

    assert result.deactivated_documents == 1
    assert result.stats.active_documents == 1
    assert result.stats.documents == 3


def test_invalid_sync_mode_is_rejected() -> None:
    _, service = _service()
    try:
        service.sync((), mode="unknown")
    except ValueError as exc:
        assert "incremental or full" in str(exc)
    else:
        raise AssertionError("invalid mode should fail")
