"""Direct-answer must post an IM text reply even if session side-effects fail."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi.testclient import TestClient

from project_lens.domain.models import ProjectAnswer, ProjectRef
from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopResult
from project_lens.integrations.feishu.identity import parse_project_bindings
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
                    "hermes_ok": True,
                    "tool_names": ["projectlens_search_context"],
                },
            },
            tool_names=("projectlens_search_context",),
            loop_id=kwargs["loop_id"],
            trace_id=kwargs["trace_id"],
            verified_answer=ProjectAnswer(
                project=project,
                status="unknown",
                business_summary="这是 Hermes 直接回答正文，应出现在飞书群文本里。",
                technical_summary="tech",
                unknowns=(),
            ),
        )


def _configure(app) -> None:  # noqa: ANN001
    verifier = app.state.feishu_event_service._verifier
    verifier._verification_token = LOCAL_TOKEN
    verifier._signing_secret = None
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


def _direct_answer_payload(*, event_id: str, session_id: UUID) -> dict[str, object]:
    return {
        "schema": "2.0",
        "token": LOCAL_TOKEN,
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "tenant_key": "demo",
        },
        "event": {
            "operator": {"open_id": "u1", "user_id": "u1"},
            "action": {
                "tag": "button",
                "value": {
                    "action": "context_direct_answer",
                    "session_id": str(session_id),
                },
            },
            "context": {"open_chat_id": "chat-1", "chat_type": "group"},
        },
    }


def _text_bodies(app) -> list[str]:  # noqa: ANN001
    return [
        str(item.content.get("text") or "")
        for item in app.state.feishu_messenger.messages
        if item.message_type == "text"
    ]


def test_context_direct_answer_posts_text_reply_to_feishu() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)

    preview = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="da-preview", text="介绍一下项目"),
    )
    assert preview.status_code == 200
    assert preview.json()["status"] == "accepted"

    project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    session = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=project,
    )
    assert session is not None
    assert session.pending_send is not None

    answer = client.post(
        "/api/v1/feishu/events",
        json=_direct_answer_payload(event_id="da-click", session_id=session.session_id),
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "accepted"
    assert "run_id" in body

    texts = _text_bodies(app)
    assert texts, "expected a Feishu text IM after 直接回答"
    assert any("Hermes 直接回答正文" in text for text in texts)


def test_direct_answer_still_posts_when_attach_hermes_fails(monkeypatch) -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)
    client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="da-attach-preview", text="介绍一下项目"),
    )
    project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    session = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=project,
    )
    assert session is not None

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise ValueError("session is not the active branch for this binding; switch first")

    monkeypatch.setattr(
        app.state.conversation_service,
        "attach_hermes_tool_loop",
        _boom,
    )

    response = client.post(
        "/api/v1/feishu/events",
        json=_direct_answer_payload(
            event_id="da-attach-click",
            session_id=session.session_id,
        ),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    texts = _text_bodies(app)
    assert any("Hermes 直接回答正文" in text for text in texts)


def test_direct_answer_still_posts_when_propose_memory_raises(monkeypatch) -> None:
    """propose_memory_from_answer used to sit outside the try; long FACT crashed send."""

    app = create_app()
    _configure(app)
    client = TestClient(app)
    client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="da-propose-preview", text="介绍一下项目"),
    )
    project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    session = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=project,
    )
    assert session is not None

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise ValueError("claim_text too long")

    monkeypatch.setattr(
        "project_lens.integrations.feishu.service.propose_memory_from_answer",
        _boom,
    )
    app.state.feishu_event_service._memory_gateway = object()

    response = client.post(
        "/api/v1/feishu/events",
        json=_direct_answer_payload(
            event_id="da-propose-click",
            session_id=session.session_id,
        ),
    )
    assert response.status_code == 200
    texts = _text_bodies(app)
    assert any("Hermes 直接回答正文" in text for text in texts)
    assert not any("发送到飞书失败" in text for text in texts)


def test_direct_answer_still_posts_when_memory_proposal_raises(monkeypatch) -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)
    client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="da-mem-preview", text="介绍一下项目"),
    )
    project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    session = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id="chat-1",
        user_id="u1",
        project=project,
    )
    assert session is not None

    class _ExplodingGateway:
        def create_memory_proposal(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("memory store unavailable")

    monkeypatch.setattr(
        app.state.feishu_event_service,
        "_memory_gateway",
        _ExplodingGateway(),
    )
    # Force a FACT+evidence answer so propose_memory_from_answer returns a candidate.
    from project_lens.domain.models import (
        Claim,
        ClaimType,
        Evidence,
        EvidenceGrade,
        EvidenceType,
        SourceRef,
    )
    from datetime import datetime, timezone

    async def _answer_with_fact(**kwargs):  # noqa: ANN003
        project = kwargs["project"]
        evidence = Evidence(
            type=EvidenceType.DOCUMENT,
            project=project,
            source=SourceRef(system="local", source_id="readme"),
            content="readme",
            observed_at=datetime.now(timezone.utc),
            access_scope="project:payment:read",
            content_hash="1234567890abcdefaa",
        )
        return FeishuHermesToolLoopResult(
            ok=True,
            envelope={
                "ok": True,
                "audit_ref": {
                    "allow_apply": False,
                    "loop_id": str(kwargs["loop_id"]),
                    "trace_id": str(kwargs["trace_id"]),
                    "hermes_ok": True,
                    "tool_names": [],
                },
            },
            tool_names=(),
            loop_id=kwargs["loop_id"],
            trace_id=kwargs["trace_id"],
            verified_answer=ProjectAnswer(
                project=project,
                status="ok",
                business_summary="带引用的成功回答，应照样发到飞书。",
                technical_summary="tech",
                claims=(
                    Claim(
                        text="payment 是订单服务",
                        type=ClaimType.FACT,
                        evidence_ids=(evidence.id,),
                        grade=EvidenceGrade.B,
                    ),
                ),
                evidence=(evidence,),
                unknowns=(),
            ),
        )

    app.state.feishu_hermes_tool_loop_bridge.answer = _answer_with_fact

    response = client.post(
        "/api/v1/feishu/events",
        json=_direct_answer_payload(
            event_id="da-mem-click",
            session_id=session.session_id,
        ),
    )
    assert response.status_code == 200
    texts = _text_bodies(app)
    assert any("带引用的成功回答" in text for text in texts)
