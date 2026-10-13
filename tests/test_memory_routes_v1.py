from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from project_lens.domain.memory import MemoryProposal, MemoryType
from project_lens.domain.models import ProjectRef
from project_lens.context.source_records import SourceRecord, SourceType
from project_lens.main import create_app
from tests.conftest import project_agent_headers


def _client() -> TestClient:
    return TestClient(create_app())


def _headers(*, actor_id: str = "u1", chat_id: str = "chat-1", tenant_key: str = "demo") -> dict[str, str]:
    return project_agent_headers(actor_id=actor_id, chat_id=chat_id, tenant_key=tenant_key)


def _memory(
    client: TestClient,
    *,
    text: str,
    subject: str = "payment-service",
    visibility_scope: tuple[str, ...] = (),
):
    project = ProjectRef(tenant_id="demo", project_id="payment")
    proposal = MemoryProposal(
        project=project,
        proposed_by="u1",
        claim_text=text,
        memory_type=MemoryType.DECISION,
        subject=subject,
        claim_slot="transport",
        evidence_ids=(uuid4(),),
        visibility_scope=visibility_scope,
    )
    client.app.state.memory_store.create_proposal(proposal)
    _, memory = client.app.state.memory_store.decide_proposal(
        proposal.id, approved=True, decided_by="u1"
    )
    assert memory is not None
    return memory


def test_v1_memory_search_detail_summary_and_versions_are_authorized() -> None:
    client = _client()
    memory = _memory(client, text="Payment callbacks use Kafka", subject="payment-service")

    search = client.get(
        "/api/v1/projects/demo/payment/memories/search",
        params={"query": "Kafka", "limit": 999},
        headers=_headers(),
    )
    assert search.status_code == 200
    assert search.json()["returned_count"] == 1
    assert search.json()["memories"][0]["memory_id"] == str(memory.id)

    detail = client.get(
        f"/api/v1/projects/demo/payment/memories/{memory.id}",
        headers=_headers(),
    )
    assert detail.status_code == 200
    assert detail.json()["memory"]["id"] == str(memory.id)

    summary = client.get(
        "/api/v1/projects/demo/payment/memory-summary",
        headers=_headers(),
    )
    assert summary.status_code == 200
    assert summary.json()["entries"]

    versions = client.get(
        f"/api/v1/projects/demo/payment/memories/{memory.id}/versions",
        headers=_headers(),
    )
    assert versions.status_code == 200
    assert len(versions.json()["versions"]) == 1


def test_v1_memory_routes_reject_cross_tenant_actor() -> None:
    client = _client()
    response = client.get(
        "/api/v1/projects/demo/payment/memories/search",
        params={"query": "Kafka"},
        headers=_headers(tenant_key="other"),
    )
    assert response.status_code == 403


def test_legacy_memory_list_route_filters_visibility_before_returning_rows() -> None:
    client = _client()
    _memory(
        client,
        text="private payment decision",
        visibility_scope=("actor:another-user",),
    )

    response = client.get(
        "/api/v1/projects/demo/payment/memories",
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json() == []


def test_trusted_proposal_route_resolves_authoritative_source_metadata() -> None:
    client = _client()
    client.app.state.source_record_store.put(
        SourceRecord(
            source_id="knowledge/requirements",
            source_type=SourceType.FEISHU_DOCUMENT,
            tenant_id="demo",
            project_id="payment",
            revision="r1",
            observed_at=datetime.now(timezone.utc),
            access_scope="knowledge/",
            content_hash="1234567890abcdef",
            content="Payment callback requirement",
        )
    )
    response = client.post(
        "/api/v1/projects/demo/payment/memory-proposals",
        json={
            "claim_text": "Payment callbacks use Kafka",
            "memory_type": "decision",
            "evidence_ids": [str(uuid4())],
            "source_refs": [
                {
                    "tenant_id": "demo",
                    "project_id": "payment",
                    "source_id": "knowledge/requirements",
                    "revision": "r1",
                    "content_hash": "spoofed-hash",
                    "source_type": "code",
                }
            ],
            "subject": "payment-service",
            "claim_slot": "transport",
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    proposal = response.json()
    assert proposal["proposed_by"] == "u1"
    assert proposal["source_refs"][0]["content_hash"] == "1234567890abcdef"
    assert proposal["source_refs"][0]["source_type"] == "feishu_document"
    approved = client.post(
        f"/api/v1/memory-proposals/{proposal['id']}/decision",
        json={"approved": True},
        headers=_headers(),
    )
    assert approved.status_code == 200
    assert approved.json()["memory"]["content_hash"]


def test_trusted_proposal_route_rejects_cross_project_source() -> None:
    client = _client()
    response = client.post(
        "/api/v1/projects/demo/payment/memory-proposals",
        json={
            "claim_text": "Payment callbacks use Kafka",
            "evidence_ids": [str(uuid4())],
            "source_refs": [
                {
                    "tenant_id": "demo",
                    "project_id": "crm",
                    "source_id": "knowledge/requirements",
                    "revision": "r1",
                }
            ],
        },
        headers=_headers(),
    )
    assert response.status_code == 422
    assert "MEMORY_INVALID_PROVENANCE" in response.json()["detail"]


def test_memory_revoke_and_review_resolution_are_auditable() -> None:
    client = _client()
    memory = _memory(client, text="Payment callbacks use Kafka", subject="payment-service")
    revoked = client.post(
        f"/api/v1/memories/{memory.id}/revoke",
        json={"reason": "source no longer authoritative"},
        headers=_headers(),
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["memory"]["status"] == "revoked"
    review_id = revoked.json()["review_id"]

    listed = client.get(
        "/api/v1/projects/demo/payment/memory-reviews",
        headers=_headers(),
    )
    assert listed.status_code == 200
    assert any(item["review_id"] == review_id for item in listed.json()["reviews"])

    resolved = client.post(
        f"/api/v1/memories/{memory.id}/reviews/{review_id}/resolve",
        json={"resolution": "confirmed revoked"},
        headers=_headers(),
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["review"]["status"] == "resolved"
