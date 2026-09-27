"""ProjectLens tool envelope API for Hermes runtime integration."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from project_lens.main import create_app
from project_lens.context.memory_store import SQLiteMemoryStore
from project_lens.domain.memory import MemoryProposal, MemoryType
from project_lens.domain.models import ProjectRef


def _client() -> TestClient:
    return TestClient(create_app())


def _headers(
    *,
    actor_id: str = "u1",
    chat_id: str = "chat-1",
    tenant_key: str = "demo",
) -> dict[str, str]:
    from tests.conftest import project_agent_headers

    return project_agent_headers(
        actor_id=actor_id,
        chat_id=chat_id,
        tenant_key=tenant_key,
    )


def _project_payload(project_id: str = "payment") -> dict[str, str]:
    return {
        "tenant_id": "demo",
        "project_id": project_id,
    }


def _call_payload(
    *,
    tool_name: str,
    arguments: dict[str, object],
    project_id: str = "payment",
    user_id: str = "u1",
    chat_id: str = "chat-1",
) -> dict[str, object]:
    return {
        "tool_name": tool_name,
        "project": _project_payload(project_id),
        "user_id": user_id,
        "chat_id": chat_id,
        "arguments": arguments,
    }


def test_project_agent_tools_list_is_read_only_and_namespaced() -> None:
    response = _client().get("/api/v1/project-agent/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    names = {item["name"] for item in body["tools"]}
    assert names == {
        "projectlens_get_citation_body",
        "projectlens_search_project_memory",
        "projectlens_get_memory_detail",
        "projectlens_search_project_history",
        "projectlens_get_run_detail",
        "projectlens_search_context",
        "projectlens_read_project_file",
        "projectlens_list_project_files",
        "projectlens_query_graph",
        "projectlens_authorized_evidence",
        "projectlens_list_knowledge_gaps",
    }
    assert all(item["category"] == "read" for item in body["tools"])
    assert all(item["allow_apply"] is False for item in body["tools"])
    assert not any("deploy" in name or "apply" in name or "restart" in name for name in names)


def test_project_agent_tool_call_search_context_returns_envelope() -> None:
    response = _client().post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_context",
            arguments={"query": "create_order coupon", "limit": 3},
        ),
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["tool_name"] == "projectlens_search_context"
    assert body["internal_tool_name"] == "search_context"
    assert body["tool_result_id"]
    assert body["summary"]
    assert body["audit_ref"]["allow_apply"] is False
    assert body["audit_ref"]["project_id"] == "payment"
    assert body["audit_ref"]["tool_name"] == "projectlens_search_context"
    assert body["visibility_scope"]["project_id"] == "payment"
    assert isinstance(body["citations"], list)
    assert isinstance(body["evidence_refs"], list)
    assert "content" not in body


def test_project_agent_memory_search_and_exact_detail_are_bounded_and_scoped() -> None:
    client = _client()
    app = client.app
    store = app.state.memory_store
    project = ProjectRef(tenant_id="demo", project_id="payment")
    proposal = MemoryProposal(
        project=project,
        proposed_by="u1",
        claim_text="支付回调必须通过 Kafka consumer lag 监控。",
        memory_type=MemoryType.RISK,
        evidence_ids=(),
    )
    # Governance requires evidence; use a deterministic UUID as a citation ref.
    from uuid import uuid4
    proposal = proposal.model_copy(update={"evidence_ids": (uuid4(),)})
    store.create_proposal(proposal)
    _updated, memory = store.decide_proposal(proposal.id, approved=True, decided_by="u1")
    assert memory is not None

    searched = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_project_memory",
            arguments={"query": "Kafka consumer lag", "limit": 999},
        ),
        headers=_headers(),
    ).json()
    assert searched["ok"] is True
    assert searched["result"]["returned_count"] <= 8
    assert searched["result"]["memories"]
    card = searched["result"]["memories"][0]
    assert card["memory_id"] == str(memory.id)
    assert searched["audit_ref"]["memory_retrieval"] is True

    detail = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_get_memory_detail",
            arguments={"memory_id": str(memory.id)},
        ),
        headers=_headers(),
    ).json()
    assert detail["ok"] is True
    assert detail["result"]["memory_id"] == str(memory.id)
    assert detail["result"]["text"] == memory.text
    assert detail["audit_ref"]["retrieval_mode"] == "exact"

    cross_project = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_get_memory_detail",
            project_id="crm",
            arguments={"memory_id": str(memory.id)},
        ),
        headers=_headers(actor_id="u-shared-1"),
    ).json()
    assert cross_project["ok"] is False
    assert cross_project["error_code"] == "MEMORY_NOT_FOUND"


def test_project_agent_history_search_and_detail_are_bounded() -> None:
    client = _client()
    app = client.app
    run = app.state.run_service.create(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        user_id="u1",
        question="Kafka consumer lag payment callback",
        channel_id="chat-1",
    )
    result = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_project_history",
            arguments={"query": "Kafka consumer lag", "limit": 999},
        ),
        headers=_headers(),
    ).json()
    assert result["ok"] is True
    assert result["result"]["returned_count"] <= 8
    assert result["result"]["history"]
    assert result["result"]["history"][0]["run_id"] == str(run.id)

    detail = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_get_run_detail",
            arguments={"run_id": str(run.id)},
        ),
        headers=_headers(),
    ).json()
    assert detail["ok"] is True
    assert detail["result"]["run_id"] == str(run.id)
    assert detail["result"]["raw_tool_arguments_available"] is False


def test_project_agent_history_search_defaults_to_session_date_window() -> None:
    """S07: omitted from/to reuse active conversation date window."""

    from datetime import datetime, timezone

    client = _client()
    app = client.app
    project = ProjectRef(tenant_id="demo", project_id="payment")
    old = app.state.run_service.create(
        project=project,
        user_id="u1",
        question="old kafka lag topic",
        channel_id="chat-1",
    )
    # Force old timestamps outside the window.
    old = old.model_copy(
        update={
            "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
    )
    app.state.run_service._repository.add(old)  # noqa: SLF001 - test fixture

    new = app.state.run_service.create(
        project=project,
        user_id="u1",
        question="new kafka lag topic",
        channel_id="chat-1",
    )

    conversation = app.state.conversation_service
    session = conversation.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=project,
    )
    session = conversation.set_date_window(
        session,
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=None,
        refresh_pending=False,
    )
    assert session.date_window is not None

    result = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_project_history",
            arguments={"query": "kafka lag", "limit": 8},
        ),
        headers=_headers(actor_id="u1"),
    ).json()
    assert result["ok"] is True
    run_ids = {item["run_id"] for item in result["result"]["history"]}
    assert str(new.id) in run_ids
    assert str(old.id) not in run_ids


def test_project_agent_get_citation_body_is_scoped_to_binding() -> None:
    client = _client()
    app = client.app
    conversation = app.state.conversation_service
    project = ProjectRef(tenant_id="demo", project_id="payment")
    session = conversation.get_or_create(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=project,
    )
    session = conversation.record_turn(
        session,
        user_id="u1",
        text="citation-body-for-tool-test",
        rewritten_question=None,
        run_id=uuid4(),
    )
    citation_id = session.citations[0].citation_id

    ok = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_get_citation_body",
            arguments={"citation_id": citation_id},
        ),
        headers=_headers(actor_id="u1"),
    ).json()
    assert ok["ok"] is True
    assert ok["internal_tool_name"] == "get_citation_body"
    assert ok["result"]["body"] == "citation-body-for-tool-test"
    assert ok["result"]["body_available"] is True

    missing = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_get_citation_body",
            arguments={"citation_id": "missing-citation"},
        ),
        headers=_headers(actor_id="u1"),
    ).json()
    assert missing["ok"] is False
    assert missing["error_code"] == "CITATION_NOT_FOUND"

    wrong_chat = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_get_citation_body",
            arguments={"citation_id": citation_id},
            chat_id="other-chat",
        ),
        headers=_headers(actor_id="u1", chat_id="other-chat"),
    ).json()
    assert wrong_chat["ok"] is False
    assert wrong_chat["error_code"] == "CITATION_NOT_FOUND"


def test_project_agent_history_search_honors_date_bounds() -> None:
    client = _client()
    app = client.app
    run = app.state.run_service.create(
        project=ProjectRef(tenant_id="demo", project_id="payment"),
        user_id="u1",
        question="historical callback check",
        channel_id="chat-1",
    )
    excluded = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_project_history",
            arguments={"query": "historical callback", "to": "2000-01-01"},
        ),
        headers=_headers(),
    ).json()
    assert excluded["ok"] is True
    assert excluded["result"]["history"] == []

    included = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_project_history",
            arguments={"query": "historical callback", "from": "2000-01-01"},
        ),
        headers=_headers(),
    ).json()
    assert included["ok"] is True
    assert included["result"]["history"][0]["run_id"] == str(run.id)

    invalid = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_project_history",
            arguments={"query": "historical callback", "from": "not-a-date"},
        ),
        headers=_headers(),
    ).json()
    assert invalid["ok"] is False
    assert invalid["error_code"] == "INVALID_ARGUMENTS"


def test_project_agent_tool_call_unknown_and_write_tools_are_denied() -> None:
    client = _client()
    for tool_name in (
        "projectlens_apply_patch",
        "projectlens_create_pr",
        "projectlens_deploy",
        "shell",
        "unknown_tool",
    ):
        response = client.post(
            "/api/v1/project-agent/tools/call",
            json=_call_payload(tool_name=tool_name, arguments={}),
            headers=_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["error_code"] in {"TOOL_NOT_ALLOWED", "UNKNOWN_TOOL"}
        assert body["audit_ref"]["allow_apply"] is False
        assert "retry" in body["agent_recovery_hint"].lower() or "read-only" in body[
            "agent_recovery_hint"
        ].lower()


def test_project_agent_tool_call_read_file_respects_role_chat_scope() -> None:
    client = _client()
    allowed = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_read_project_file",
            arguments={"path": "src/order_service.py"},
        ),
        headers=_headers(),
    )
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["ok"] is True
    assert body["citations"]
    assert all("content" not in cite for cite in body["citations"])

    denied = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_read_project_file",
            arguments={"path": "secrets/prod.env"},
        ),
        headers=_headers(),
    )
    assert denied.status_code == 200
    error = denied.json()
    assert error["ok"] is False
    assert error["error_code"] in {"TOOL_EXECUTION_FAILED", "ACCESS_DENIED"}
    assert error["audit_ref"]["allow_apply"] is False


def test_project_agent_tool_call_can_switch_project_space() -> None:
    client = _client()
    response = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_context",
            project_id="crm",
            arguments={"query": "contact", "limit": 3},
        ),
        headers=_headers(actor_id="u-shared-1"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["project"]["project_id"] == "crm"
    assert body["audit_ref"]["project_id"] == "crm"
    assert body["visibility_scope"]["project_id"] == "crm"


def test_project_agent_tool_call_query_graph_returns_envelope() -> None:
    response = _client().post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_query_graph",
            arguments={"limit": 5},
        ),
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["tool_name"] == "projectlens_query_graph"
    assert body["internal_tool_name"] == "query_graph"
    assert body["summary"]
    assert "path" in body["summary"].lower() or "query_graph" in body["summary"]
    assert body["audit_ref"]["allow_apply"] is False
    assert isinstance(body["citations"], list)
    assert isinstance(body["evidence_refs"], list)
    assert "agent_recovery_hint" not in body or body.get("ok") is True


def test_project_agent_tool_call_list_knowledge_gaps_returns_envelope() -> None:
    response = _client().post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_list_knowledge_gaps",
            arguments={},
        ),
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["tool_name"] == "projectlens_list_knowledge_gaps"
    assert body["internal_tool_name"] == "list_knowledge_gaps"
    assert body["summary"]
    assert "gap" in body["summary"].lower()
    assert body["audit_ref"]["allow_apply"] is False
    assert isinstance(body["citations"], list)
    assert isinstance(body["evidence_refs"], list)


def test_project_agent_tool_call_errors_include_agent_recovery_hint() -> None:
    response = _client().post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_grep_project",
            arguments={"query": "secret"},
        ),
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] in {"UNKNOWN_TOOL", "TOOL_NOT_ALLOWED"}
    assert body["agent_recovery_hint"]
    assert body["audit_ref"]["allow_apply"] is False


def test_projectlens_is_default_indexed_project_space() -> None:
    client = _client()
    response = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_context",
            project_id="projectlens",
            arguments={"query": "Tool Envelope Hermes", "limit": 5},
        ),
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["project"]["project_id"] == "projectlens"
    assert body["audit_ref"]["allow_apply"] is False
    assert body["summary"]
    assert isinstance(body["citations"], list)

    # Demo spaces remain available.
    payment = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_context",
            project_id="payment",
            arguments={"query": "create_order", "limit": 2},
        ),
        headers=_headers(),
    )
    crm = client.post(
        "/api/v1/project-agent/tools/call",
        json=_call_payload(
            tool_name="projectlens_search_context",
            project_id="crm",
            arguments={"query": "contact", "limit": 2},
        ),
        headers=_headers(actor_id="u-shared-1"),
    )
    assert payment.status_code == 200 and payment.json()["ok"] is True
    assert crm.status_code == 200 and crm.json()["ok"] is True
    assert payment.json()["project"]["project_id"] == "payment"
    assert crm.json()["project"]["project_id"] == "crm"


def test_hermes_plugin_defaults_to_projectlens() -> None:
    from project_lens.integrations.hermes_plugin.config import ProjectLensPluginConfig

    config = ProjectLensPluginConfig()
    assert config.default_project_id == "projectlens"
    assert config.project_for_chat(None)["project_id"] == "projectlens"
