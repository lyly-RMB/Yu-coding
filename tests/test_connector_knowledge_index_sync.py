from __future__ import annotations

from datetime import datetime, timezone

import pytest

from project_lens.application.connector_sync import ConnectorSyncService
from project_lens.application.connector_sync_status import InMemoryConnectorSyncStateStore
from project_lens.context.connectors.base import SyncBatch
from project_lens.context.store import InMemoryEvidenceIndex
from project_lens.domain.models import Evidence, EvidenceType, ProjectRef, SourceRef


def _evidence(source_id: str = "doc-1") -> Evidence:
    return Evidence(
        type=EvidenceType.DOCUMENT,
        project=ProjectRef(tenant_id="t1", project_id="p1"),
        source=SourceRef(system="docs", source_id=source_id),
        content="支付回调必须幂等。",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:p1:read",
        content_hash=("hash-" + source_id).encode().hex()[:64].ljust(64, "0"),
    )


class Connector:
    name = "docs"

    def __init__(self, batch: SyncBatch | None = None, error: Exception | None = None):
        self.batch = batch or SyncBatch(added=(_evidence(),))
        self.error = error

    async def sync(self, cursor=None):
        if self.error:
            raise self.error
        return self.batch


class IndexSyncSpy:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[tuple[Evidence, ...], str]] = []

    def sync(self, evidence, *, mode):
        if self.error:
            raise self.error
        items = tuple(evidence)
        self.calls.append((items, mode))
        return type(
            "SyncResult",
            (),
            {
                "build": type(
                    "Build",
                    (),
                    {"chunks_written": len(items)},
                )(),
            },
        )()


@pytest.mark.asyncio
async def test_successful_connector_sync_updates_knowledge_index() -> None:
    spy = IndexSyncSpy()
    service = ConnectorSyncService(
        evidence_index=InMemoryEvidenceIndex(),
        state_store=InMemoryConnectorSyncStateStore(),
        knowledge_index_sync=spy,
    )

    result = await service.sync_connector(Connector(), project=ProjectRef(tenant_id="t1", project_id="p1"))

    assert result.ok is True
    assert result.knowledge_indexed_count == 1
    assert result.knowledge_index_error is None
    assert len(spy.calls) == 1
    assert spy.calls[0][1] == "incremental"


@pytest.mark.asyncio
async def test_knowledge_index_failure_does_not_fail_evidence_sync() -> None:
    spy = IndexSyncSpy(RuntimeError("embedding unavailable"))
    service = ConnectorSyncService(
        evidence_index=InMemoryEvidenceIndex(),
        state_store=InMemoryConnectorSyncStateStore(),
        knowledge_index_sync=spy,
    )

    result = await service.sync_connector(Connector(), project=ProjectRef(tenant_id="t1", project_id="p1"))

    assert result.ok is True
    assert result.added_count == 1
    assert result.knowledge_indexed_count == 0
    assert result.knowledge_index_error == "RuntimeError"


@pytest.mark.asyncio
async def test_connector_failure_does_not_call_knowledge_index() -> None:
    spy = IndexSyncSpy()
    service = ConnectorSyncService(
        evidence_index=InMemoryEvidenceIndex(),
        state_store=InMemoryConnectorSyncStateStore(),
        knowledge_index_sync=spy,
    )

    result = await service.sync_connector(
        Connector(error=RuntimeError("upstream down")),
        project=ProjectRef(tenant_id="t1", project_id="p1"),
    )

    assert result.ok is False
    assert spy.calls == []
