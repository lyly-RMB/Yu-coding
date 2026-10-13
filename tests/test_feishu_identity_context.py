from __future__ import annotations

import json

from fastapi.testclient import TestClient

from project_lens.main import create_app

LOCAL_TOKEN = "project-lens-local-token"


def _configure(app) -> None:
    from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger

    verifier = app.state.feishu_event_service._verifier
    verifier._signing_secret = None
    verifier._verification_token = LOCAL_TOKEN
    from project_lens.domain.models import ProjectRef
    from project_lens.integrations.feishu.identity import ConfigurableFeishuIdentityMapper

    project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    app.state.feishu_event_service._identity_mapper = ConfigurableFeishuIdentityMapper(
        bindings={("demo", "chat-1"): project}
    )
    messenger = RecordingFeishuMessenger()
    app.state.feishu_messenger = messenger
    app.state.feishu_event_service._messenger = messenger
    # Event path may prepare a Hermes run; keep it deterministic and offline.
    from project_lens.domain.models import ProjectAnswer
    from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopResult

    class _FakeBridge:
        async def answer(self, **kwargs):  # noqa: ANN003
            project = kwargs["project"]
            return FeishuHermesToolLoopResult(
                ok=True,
                envelope={"ok": True, "audit_ref": {"allow_apply": False}},
                tool_names=(),
                loop_id=kwargs["loop_id"],
                trace_id=kwargs["trace_id"],
                verified_answer=ProjectAnswer(
                    project=project,
                    status="unknown",
                    business_summary="ok",
                    technical_summary="ok",
                    unknowns=("none",),
                ),
            )

    app.state.feishu_hermes_tool_loop_bridge.answer = _FakeBridge().answer


def _payload(
    *,
    event_id: str,
    text: str = "介绍一下这个项目",
    tenant_key: str = "demo",
    chat_id: str = "chat-1",
    chat_type: str = "group",
    open_id: str | None = "u_dev",
) -> dict[str, object]:
    sender_id: dict[str, str] = {}
    if open_id is not None:
        sender_id["open_id"] = open_id
    return {
        "schema": "2.0",
        "token": LOCAL_TOKEN,
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": tenant_key,
        },
        "event": {
            "sender": {"sender_id": sender_id},
            "message": {
                "message_id": f"m-{event_id}",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_feishu_rejects_missing_open_id() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_payload(event_id="missing-open-id", open_id=None),
    )

    assert response.status_code == 400
    assert "open_id" in response.json()["detail"]


def test_feishu_rejects_missing_chat_type() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)
    payload = _payload(event_id="missing-chat-type")
    message = payload["event"]["message"]  # type: ignore[index]
    del message["chat_type"]

    response = client.post("/api/v1/feishu/events", json=payload)

    assert response.status_code == 400
    assert "chat_type" in response.json()["detail"]


def test_feishu_persists_chat_type_on_actor_context() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_payload(event_id="p2p-chat", chat_type="p2p", chat_id="chat-1"),
    )

    assert response.status_code == 200
    context = app.state.feishu_event_service._identity_mapper.resolve(
        tenant_key="demo",
        chat_id="chat-1",
        user_id="u_dev",
        chat_type="p2p",
    )
    assert context.actor.chat_type == "p2p"
    assert context.actor.source == "feishu_event"
    assert context.actor.authenticated is True


def test_project_binding_exposes_explicit_digest_recipients_only() -> None:
    from project_lens.domain.models import ProjectRef
    from project_lens.integrations.feishu.identity import parse_project_bindings

    project = ProjectRef(tenant_id="demo", project_id="payment")
    mapper = parse_project_bindings(
        json.dumps({
            "bindings": [{
                "tenant_key": "feishu-demo",
                "chat_id": "oc_payment",
                "project_id": "payment",
                "owner_mappings": {"u_manager": "ou_manager"},
            }],
        }),
        default_project=project,
        registered_projects=(project,),
    )
    assert mapper.project_chat_id(project) == "oc_payment"
    assert mapper.owner_open_id(project, "u_manager") == "ou_manager"
    assert mapper.owner_open_id(project, "u_unknown") is None
