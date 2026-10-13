from datetime import datetime, timezone

from fastapi.testclient import TestClient

from project_lens.application.feishu_doc_sync import FeishuDocumentSyncService
from project_lens.application.feishu_doc_sync_status import InMemoryFeishuDocSyncStatusStore
from project_lens.domain.feishu_doc_sync import FeishuDocSyncStatus, FeishuDocSyncStatusValue
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
from project_lens.integrations.feishu.identity import parse_project_bindings
from project_lens.main import create_app
from tests.test_feishu_doc_sync import RecordingTransport
from project_lens.integrations.feishu.docs_client import FeishuDocClient
from project_lens.integrations.feishu.http_adapter import FeishuTenantTokenProvider


def _configure_feishu(app) -> None:
    verifier = app.state.feishu_event_service._verifier
    verifier._verification_token = "project-lens-local-token"
    verifier._signing_secret = None
    app.state.feishu_event_service._identity_mapper = parse_project_bindings(
        "",
        default_project=ProjectRef(
            tenant_id="demo",
            project_id="payment",
            service="order-service",
            environment="production",
        ),
        allow_demo_fallback=True,
    )
    app.state.feishu_messenger = RecordingFeishuMessenger()
    app.state.feishu_event_service._messenger = app.state.feishu_messenger


def test_sync_status_api_returns_recorded_statuses() -> None:
    app = create_app()
    store = InMemoryFeishuDocSyncStatusStore()
    project = ProjectRef(tenant_id="demo", project_id="payment", service="order-service")
    store.upsert(
        FeishuDocSyncStatus(
            project=project,
            doc_token="docx_a",
            revision="3",
            last_synced_at=datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
            status=FeishuDocSyncStatusValue.SUCCESS,
            title="Owners",
        )
    )
    store.upsert(
        FeishuDocSyncStatus(
            project=project,
            doc_token="docx_b",
            revision="1",
            last_synced_at=datetime(2026, 8, 2, 9, tzinfo=timezone.utc),
            status=FeishuDocSyncStatusValue.FAILED,
            error="not found",
            title="Broken",
        )
    )
    app.state.feishu_doc_sync_status_store = store
    client = TestClient(app)

    forbidden = client.post(
        "/api/v1/projects/feishu-docs/sync-status",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": [],
        },
    )
    assert forbidden.status_code == 403

    response = client.post(
        "/api/v1/projects/feishu-docs/sync-status",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": ["project:payment:read"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success_count"] == 1
    assert body["failed_count"] == 1
    assert body["statuses"][0]["doc_token"] == "docx_a"
    assert body["statuses"][0]["revision"] == "3"


def test_feishu_doc_sync_status_shortcut_posts_status_card() -> None:
    app = create_app()
    _configure_feishu(app)
    store = InMemoryFeishuDocSyncStatusStore()
    project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    store.upsert(
        FeishuDocSyncStatus(
            project=project,
            doc_token="docx_payment_owners",
            revision="9",
            last_synced_at=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
            status=FeishuDocSyncStatusValue.SUCCESS,
            title="Payment service ownership",
            owner_user_id="ou_ada",
            doc_url="https://feishu.cn/docx/docx_payment_owners",
            access_scope="project:payment:read",
            last_success_revision="9",
        )
    )
    store.upsert(
        FeishuDocSyncStatus(
            project=project,
            doc_token="docx_skip",
            revision="2",
            last_synced_at=datetime(2026, 8, 2, 11, tzinfo=timezone.utc),
            status=FeishuDocSyncStatusValue.SKIPPED,
            title="Unchanged doc",
            owner_user_id="ou_bob",
            doc_url="https://feishu.cn/docx/docx_skip",
            access_scope="project:payment:read",
            last_success_revision="2",
        )
    )
    app.state.feishu_doc_sync_status_store = store
    app.state.feishu_event_service._sync_status_store = store
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": "project-lens-local-token",
            "header": {
                "event_id": "doc-sync-status-event",
                "event_type": "im.message.receive_v1",
                "tenant_key": "demo",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "feishu-user-1", "user_id": "feishu-user-1"}},
                "message": {
                    "message_id": "m-doc-sync-status",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"文档同步状态"}',
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert "run_id" not in response.json()
    messages = app.state.feishu_messenger.messages
    assert len(messages) == 1
    card_text = "\n".join(
        element.get("content", "") for element in messages[0].content["elements"]
    )
    assert "**知识库同步视图**" in card_text
    assert "success: 1" in card_text
    assert "skipped: 1" in card_text
    assert "revision=9" in card_text
    assert "owner=ou_ada" in card_text
    assert "owner=ou_bob" in card_text
    assert "scope=project:payment:read" in card_text
    assert "url=https://feishu.cn/docx/docx_payment_owners" in card_text
    assert "Payment service ownership" in card_text
    assert "**知识库最近更新**" in card_text


def test_status_store_failure_does_not_break_normal_chat() -> None:
    app = create_app()
    _configure_feishu(app)

    class BrokenStore(InMemoryFeishuDocSyncStatusStore):
        def list_for_project(self, project: ProjectRef):
            raise RuntimeError("status db down")

    app.state.feishu_event_service._sync_status_store = BrokenStore()
    app.state.feishu_event_service._context_preview_enabled = False
    client = TestClient(app)

    # Status shortcut still returns accepted and posts a card with read error.
    status_response = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": "project-lens-local-token",
            "header": {
                "event_id": "broken-status-event",
                "event_type": "im.message.receive_v1",
                "tenant_key": "demo",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "feishu-user-1", "user_id": "feishu-user-1"}},
                "message": {
                    "message_id": "m-broken-status",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"知识库最近更新了什么"}',
                },
            },
        },
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "accepted"
    card_text = "\n".join(
        element.get("content", "")
        for element in app.state.feishu_messenger.messages[-1].content["elements"]
    )
    assert "读取同步状态失败" in card_text

    # Normal chat still creates a run.
    normal = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": "project-lens-local-token",
            "header": {
                "event_id": "normal-chat-after-status-failure",
                "event_type": "im.message.receive_v1",
                "tenant_key": "demo",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "feishu-user-1", "user_id": "feishu-user-1"}},
                "message": {
                    "message_id": "m-normal-after-status",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"负责人是谁"}',
                },
            },
        },
    )
    assert normal.status_code == 200
    assert normal.json()["status"] == "accepted"
    assert "run_id" in normal.json()


def test_sync_api_includes_statuses_in_response(monkeypatch) -> None:  # noqa: ANN001
    from tests.test_feishu_doc_sync import _allow_feishu_external

    _allow_feishu_external(monkeypatch)
    app = create_app()
    transport = RecordingTransport(
        {
            "/documents/docx_status_api/raw_content": {
                "code": 0,
                "data": {"content": "order-service owner is Ada from status api"},
            },
            "/documents/docx_status_api": {
                "code": 0,
                "data": {
                    "document": {
                        "document_id": "docx_status_api",
                        "title": "Status API Doc",
                        "revision_id": 4,
                        "owner_id": "ou_ada",
                    }
                },
            },
        }
    )
    token_provider = FeishuTenantTokenProvider(
        app_id="app",
        app_secret="secret",
        transport=transport,
    )
    store = InMemoryFeishuDocSyncStatusStore()
    app.state.feishu_doc_sync_status_store = store
    app.state.feishu_doc_sync_service = FeishuDocumentSyncService(
        client=FeishuDocClient(token_provider=token_provider, transport=transport),
        index=app.state.evidence_index,
        status_store=store,
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/projects/feishu-docs/sync",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": ["project:payment:read"],
            "doc_tokens": ["docx_status_api"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["statuses"]
    assert body["statuses"][0]["status"] == "success"
    assert body["statuses"][0]["revision"] == "4"
    assert body["statuses"][0]["title"] == "Status API Doc"
    assert body["statuses"][0]["owner_user_id"] == "ou_ada"
