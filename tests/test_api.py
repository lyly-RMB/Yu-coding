from fastapi.testclient import TestClient

from project_lens.main import create_app


TRACEBACK = """Traceback (most recent call last):
  File "order_service.py", line 16, in create_order
    coupon_id = request.coupon.id
AttributeError: 'NoneType' object has no attribute 'id'
"""


def test_health() -> None:
    client = TestClient(create_app())
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_and_get_run() -> None:
    client = TestClient(create_app())
    create_response = client.post(
        "/api/v1/runs",
        json={
            "project": {
                "tenant_id": "demo",
                "project_id": "payment",
                "service": "order-service",
                "environment": "production",
            },
            "user_id": "feishu-user-1",
            "channel_id": "feishu-group-1",
            "question": "Why did payment requests fail?",
        },
    )

    assert create_response.status_code == 202
    run_id = create_response.json()["run_id"]
    get_response = client.get(f"/api/v1/runs/{run_id}")
    assert get_response.status_code == 200
    assert get_response.json()["status"] == "accepted"
    assert get_response.json()["question"] == "Why did payment requests fail?"


def test_rejects_unknown_request_fields() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/runs",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "question": "status?",
            "unexpected": True,
        },
    )

    assert response.status_code == 422


def test_project_snapshot_endpoint_returns_authorized_project_map() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/snapshot",
        json={
            "project": {
                "tenant_id": "demo",
                "project_id": "payment",
                "service": "order-service",
                "environment": "production",
            },
            "user_id": "u1",
            "permissions": ["project:payment:read"],
        },
    )

    assert response.status_code == 200
    snapshot = response.json()
    assert "order-service" in snapshot["services"]
    assert "create_order" in snapshot["entrypoints"]
    assert "payment service" in snapshot["dependencies"]
    assert snapshot["evidence_ids"]


def test_project_snapshot_endpoint_respects_permissions() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/snapshot",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": [],
        },
    )

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["evidence_ids"] == []
    assert "缺少服务归属证据。" in snapshot["unresolved_items"]


def test_project_timeline_endpoint_returns_authorized_events() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/timeline",
        json={
            "project": {
                "tenant_id": "demo",
                "project_id": "payment",
                "service": "order-service",
                "environment": "production",
            },
            "user_id": "u1",
            "permissions": ["project:payment:read"],
            "limit": 5,
        },
    )

    assert response.status_code == 200
    events = response.json()["events"]
    assert events
    # create_app indexes config/projects sources (code/docs); full git/incident
    # fixtures are covered by tests/test_source_bootstrap.py unit registration.
    assert {event["event_type"] for event in events} >= {"code", "document"}


def test_project_timeline_endpoint_respects_permissions() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/timeline",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["events"] == []


def test_knowledge_index_status_endpoint_is_acl_filtered() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/knowledge-index/status",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": ["project:payment:read"],
        },
    )

    assert response.status_code == 200
    assert response.json()["index_stats"]["documents"] == 0

    denied = client.post(
        "/api/v1/projects/knowledge-index/status",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": [],
        },
    )
    assert denied.status_code == 403


def test_knowledge_index_sync_dry_run_reports_planned_work_without_writes() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/knowledge-index/sync",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": ["project:payment:read"],
            "mode": "full",
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["input_records"] > 0
    assert body["chunks_written"] > 0
    assert body["index_stats"]["documents"] == 0


def test_project_change_impact_endpoint_returns_authorized_summary() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/change-impact",
        json={
            "project": {
                "tenant_id": "demo",
                "project_id": "payment",
                "service": "order-service",
                "environment": "production",
            },
            "user_id": "u1",
            "permissions": ["project:payment:read"],
            "limit": 5,
        },
    )

    assert response.status_code == 200
    impact = response.json()
    assert impact["related_events"]
    assert impact["evidence_ids"]
    assert "Observed" in impact["summary"]


def test_project_change_impact_endpoint_returns_empty_without_permission() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/change-impact",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": [],
        },
    )

    assert response.status_code == 200
    impact = response.json()
    assert impact["related_events"] == []
    assert impact["evidence_ids"] == []


def test_project_knowledge_gaps_endpoint_returns_structured_report() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/knowledge-gaps",
        json={
            "project": {
                "tenant_id": "demo",
                "project_id": "payment",
                "service": "order-service",
                "environment": "production",
            },
            "user_id": "u1",
            "permissions": ["project:payment:read"],
        },
    )

    assert response.status_code == 200
    report = response.json()
    assert report["evidence_ids"]
    assert report["type_coverage"]["code"] > 0
    assert report["type_coverage"]["document"] > 0
    assert all("source_signal" in gap for gap in report["gaps"])


def test_project_knowledge_gaps_endpoint_respects_permissions() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/projects/knowledge-gaps",
        json={
            "project": {"tenant_id": "demo", "project_id": "payment"},
            "user_id": "u1",
            "permissions": [],
        },
    )

    assert response.status_code == 200
    report = response.json()
    assert report["evidence_ids"] == []
    assert report["gaps"]


def test_legacy_run_execute_is_blocked_in_hermes_app() -> None:
    client = TestClient(create_app())
    create_response = client.post(
        "/api/v1/runs",
        json={
            "project": {
                "tenant_id": "demo",
                "project_id": "payment",
                "service": "order-service",
                "environment": "production",
            },
            "user_id": "feishu-user-1",
            "channel_id": "feishu-group-1",
            "question": TRACEBACK,
        },
    )
    run_id = create_response.json()["run_id"]

    execute_response = client.post(f"/api/v1/runs/{run_id}/execute")

    assert execute_response.status_code == 409
    assert "Hermes" in execute_response.json()["detail"]
    assert client.app.state.run_service.agent_mode == "hermes"
    assert not hasattr(client.app.state.run_service, "_workflow")


def test_unknown_project_run_create_still_accepted_without_legacy_execute() -> None:
    client = TestClient(create_app())
    create_response = client.post(
        "/api/v1/runs",
        json={
            "project": {"tenant_id": "demo", "project_id": "unknown"},
            "user_id": "u1",
            "question": "What failed?",
        },
    )
    assert create_response.status_code == 202
    run_id = create_response.json()["run_id"]

    execute_response = client.post(f"/api/v1/runs/{run_id}/execute")

    assert execute_response.status_code == 409
    assert "Hermes" in execute_response.json()["detail"]
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "accepted"
