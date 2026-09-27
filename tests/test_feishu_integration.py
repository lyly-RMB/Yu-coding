"""Feishu ingress regressions for the Hermes-only production runtime."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi.testclient import TestClient

from project_lens.domain.models import ProjectAnswer, ProjectRef
from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopResult
from project_lens.integrations.feishu.identity import parse_project_bindings
from project_lens.integrations.feishu.security import build_signature
from project_lens.main import create_app


LOCAL_TOKEN = "project-lens-local-token"


class _FakeHermesBridge:
    async def answer(self, **kwargs):  # noqa: ANN003
        project = kwargs["project"]
        return FeishuHermesToolLoopResult(
            ok=True,
            envelope={
                "ok": True,
                "audit_ref": {
                    "allow_apply": False,
                    "loop_id": str(kwargs["loop_id"]),
                    "trace_id": str(kwargs["trace_id"]),
                },
            },
            tool_names=("projectlens_search_context",),
            loop_id=kwargs["loop_id"],
            trace_id=kwargs["trace_id"],
            verified_answer=ProjectAnswer(
                project=project,
                status="unknown",
                business_summary="Evidence is insufficient.",
                technical_summary="Evidence is insufficient.",
                unknowns=("Need cited evidence.",),
            ),
        )


def _configure_verifier(app, *, signing_secret: str | None = None) -> None:  # noqa: ANN001
    verifier = app.state.feishu_event_service._verifier
    verifier._verification_token = LOCAL_TOKEN
    verifier._signing_secret = signing_secret
    project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    app.state.feishu_event_service._identity_mapper = parse_project_bindings(
        "", default_project=project, allow_demo_fallback=True
    )
    app.state.feishu_messenger = RecordingFeishuMessenger()
    app.state.feishu_event_service._messenger = app.state.feishu_messenger
    app.state.feishu_hermes_tool_loop_bridge.answer = _FakeHermesBridge().answer


def _message_payload(*, event_id: str, text: str) -> dict[str, object]:
    return {
        "schema": "2.0",
        "token": LOCAL_TOKEN,
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": "demo",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "u1"}},
            "message": {
                "message_id": f"message-{event_id}",
                "chat_id": "chat-1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_feishu_url_verification_challenge() -> None:
    app = create_app()
    _configure_verifier(app)
    response = TestClient(app).post(
        "/api/v1/feishu/events",
        json={"type": "url_verification", "token": LOCAL_TOKEN, "challenge": "challenge-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-token"}


def test_feishu_signed_event_requires_valid_signature() -> None:
    app = create_app()
    _configure_verifier(app, signing_secret="secret")
    body = json.dumps(_message_payload(event_id="signed", text="介绍一下这个项目"), separators=(",", ":")).encode()
    response = TestClient(app).post(
        "/api/v1/feishu/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Lark-Request-Timestamp": "123",
            "X-Lark-Request-Nonce": "abc",
            "X-Lark-Signature": build_signature(body=body, timestamp="123", nonce="abc", signing_secret="secret"),
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_feishu_project_question_creates_real_hermes_run_and_card() -> None:
    app = create_app()
    _configure_verifier(app)
    response = TestClient(app).post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="project-question", text="介绍一下这个项目"),
    )
    assert response.status_code == 200
    run = app.state.run_service.get(UUID(response.json()["run_id"]))
    assert run is not None
    assert run.runtime == "hermes"
    assert run.entry_mode == "natural_project_question"
    assert run.hermes_loop_id is not None
    assert run.answer is not None
    assert app.state.event_sink.for_run(run.id)
    assert len(app.state.feishu_messenger.messages) == 1
    assert app.state.feishu_messenger.messages[0].message_type == "text"


def test_feishu_debug_project_command_is_hermes_and_keeps_run_id() -> None:
    app = create_app()
    _configure_verifier(app)
    response = TestClient(app).post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="debug", text="/project introduce this project"),
    )
    run = app.state.run_service.get(UUID(response.json()["run_id"]))
    assert run is not None
    assert run.runtime == "hermes"
    assert run.entry_mode == "debug_slash"
    assert run.question == "introduce this project"


def test_feishu_followup_binds_to_latest_hermes_run() -> None:
    app = create_app()
    _configure_verifier(app)
    client = TestClient(app)
    first = client.post("/api/v1/feishu/events", json=_message_payload(event_id="followup-1", text="介绍一下这个项目"))
    second = client.post("/api/v1/feishu/events", json=_message_payload(event_id="followup-2", text="那负责人是谁？"))
    first_id = UUID(first.json()["run_id"])
    second_id = UUID(second.json()["run_id"])
    project = ProjectRef(tenant_id="demo", project_id="payment", service="order-service", environment="production")
    session = app.state.conversation_store.get_by_binding(tenant_id="demo", chat_id="chat-1", user_id="u1", project=project)
    assert session is not None
    assert session.last_run_id == second_id
    assert first_id != second_id
    assert len(session.recent_turns) >= 2


def test_feishu_role_view_reuses_completed_hermes_run() -> None:
    app = create_app()
    _configure_verifier(app)
    client = TestClient(app)
    first = client.post("/api/v1/feishu/events", json=_message_payload(event_id="role-1", text="介绍一下这个项目"))
    second = client.post("/api/v1/feishu/events", json=_message_payload(event_id="role-2", text="发给产品看"))
    assert second.json()["run_id"] == first.json()["run_id"]


def test_feishu_sync_status_and_chitchat_do_not_create_agent_runs() -> None:
    app = create_app()
    _configure_verifier(app)
    client = TestClient(app)
    sync = client.post("/api/v1/feishu/events", json=_message_payload(event_id="sync", text="同步了吗？"))
    chat = client.post("/api/v1/feishu/events", json=_message_payload(event_id="chat", text="你好"))
    assert sync.json() == {"status": "accepted"}
    assert chat.json() == {"status": "accepted"}
    assert "run_id" not in sync.json()
    assert "run_id" not in chat.json()


def test_feishu_event_id_is_idempotent() -> None:
    app = create_app()
    _configure_verifier(app)
    client = TestClient(app)
    first = client.post("/api/v1/feishu/events", json=_message_payload(event_id="same", text="介绍一下这个项目"))
    second = client.post("/api/v1/feishu/events", json=_message_payload(event_id="same", text="介绍一下这个项目"))
    assert first.json()["status"] == "accepted"
    assert second.json() == {"status": "duplicate"}
    assert len(app.state.feishu_messenger.messages) == 1
