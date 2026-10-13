from uuid import UUID

from fastapi.testclient import TestClient

from project_lens.domain.models import (
    Claim,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceType,
    ProjectAnswer,
    ProjectRef,
    SourceRef,
)
from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopResult
from project_lens.integrations.feishu.identity import parse_project_bindings
from project_lens.main import create_app
from tests.conftest import project_agent_headers
from datetime import datetime, timezone


LOCAL_TOKEN = "project-lens-local-token"
TRACEBACK = """Traceback (most recent call last):
  File "order_service.py", line 16, in create_order
    coupon_id = request.coupon.id
AttributeError: 'NoneType' object has no attribute 'id'
"""


class _FakeHermesBridge:
    async def answer(self, **kwargs):  # noqa: ANN003
        project = kwargs["project"]
        evidence = Evidence(
            type=EvidenceType.CODE,
            project=project,
            source=SourceRef(system="local", source_id="src/order_service.py"),
            content="coupon_id = request.coupon.id",
            observed_at=datetime.now(timezone.utc),
            access_scope="project:payment:read",
            content_hash="1234567890abcdefaa",
        )
        answer = ProjectAnswer(
            project=project,
            skill="incident_diagnosis",
            confidence=0.8,
            status="identified",
            business_summary="下单可能因 coupon 为空失败",
            technical_summary="AttributeError on request.coupon.id",
            claims=(
                Claim(
                    text="order-service create_order fails when coupon is None",
                    type=ClaimType.FACT,
                    evidence_ids=(evidence.id,),
                    grade=EvidenceGrade.B,
                ),
            ),
            evidence=(evidence,),
            unknowns=(),
        )
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
            verified_answer=answer,
        )


def _configure(app) -> None:
    verifier = app.state.feishu_event_service._verifier
    verifier._verification_token = LOCAL_TOKEN
    verifier._signing_secret = None
    default_project = ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )
    app.state.feishu_event_service._identity_mapper = parse_project_bindings(
        "",
        default_project=default_project,
        allow_demo_fallback=True,
    )
    app.state.feishu_messenger = RecordingFeishuMessenger()
    app.state.feishu_event_service._messenger = app.state.feishu_messenger
    app.state.feishu_event_service._memory_store = app.state.memory_store
    app.state.feishu_hermes_tool_loop_bridge.answer = _FakeHermesBridge().answer
    if getattr(app.state, "hermes_runtime_service", None) is not None:
        app.state.hermes_runtime_service._bridge = app.state.feishu_hermes_tool_loop_bridge


def _message_payload(*, event_id: str, text: str) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": "demo",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "feishu-user-1", "user_id": "feishu-user-1"}},
            "message": {
                "message_id": f"msg-{event_id}",
                "chat_id": "chat-1",
                "chat_type": "group",
                "message_type": "text",
                "content": f'{{"text":"{text}"}}',
            },
        },
        "token": LOCAL_TOKEN,
    }


def _card_action_payload(
    *,
    event_id: str,
    action: str,
    proposal_id: str,
    user_id: str = "feishu-lead",
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "tenant_key": "demo",
        },
        "event": {
            "operator": {"open_id": user_id, "user_id": user_id},
            "action": {
                "tag": "button",
                "value": {
                    "action": action,
                    "proposal_id": proposal_id,
                },
            },
            "context": {"open_chat_id": "chat-1"},
        },
        "token": LOCAL_TOKEN,
    }


def _last_card(app) -> dict:
    messages = app.state.feishu_messenger.messages
    cards = [item for item in messages if item.message_type == "interactive"]
    assert cards
    return cards[-1].content


def _confirm_message(client, *, response, event_id: str) -> dict:
    result = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "header": {"event_id": event_id, "event_type": "card.action.trigger", "tenant_key": "demo"},
            "event": {
                "operator": {"open_id": "feishu-user-1"},
                "action": {"tag": "button", "value": {"action": "context_direct_answer", "session_id": response.json()["session_id"]}},
                "context": {"open_chat_id": "chat-1", "chat_type": "group"},
            },
            "token": LOCAL_TOKEN,
        },
    )
    assert result.status_code == 200
    return result.json()


def test_feishu_answer_card_offers_memory_proposal_buttons() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="memory-offer-event", text=TRACEBACK),
    )
    assert response.status_code == 200
    confirm = _confirm_message(client, response=response, event_id="memory-offer-confirm")
    card = _last_card(app)
    card_text = str(card)
    assert "memory_approve" in card_text
    assert "memory_reject" in card_text
    # Answer body and memory buttons share one interactive card.
    assert "下单可能因 coupon 为空失败" in card_text
    assert "建议沉淀为项目记忆" in card_text
    assert "项目记忆待确认" not in card_text
    interactive = [
        item
        for item in app.state.feishu_messenger.messages
        if item.message_type == "interactive"
    ]
    # After hermes completes, the last interactive must be the merged answer card
    # (no separate pending memory decision card).
    assert interactive[-1].content is card
    assert not any(
        "项目记忆待确认" in str(item.content) for item in interactive
    )

    run_id = confirm.get("run_id") or response.json().get("run_id")
    assert run_id
    run = app.state.run_service.get(UUID(run_id))
    assert run.status == "completed"
    assert run.answer is not None
    proposal_ids = [
        action.get("value", {}).get("proposal_id")
        for element in card.get("elements", [])
        if element.get("tag") == "action"
        for action in element.get("actions", [])
    ]
    assert any(proposal_ids)
    assert app.state.memory_store.get_proposal(UUID(proposal_ids[0])) is not None


def test_feishu_card_action_approves_memory_proposal() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)

    create_response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="memory-create-event", text=TRACEBACK),
    )
    assert create_response.status_code == 200
    _confirm_message(client, response=create_response, event_id="memory-create-confirm")
    card = _last_card(app)
    proposal_id = None
    for element in card.get("elements", []):
        if element.get("tag") != "action":
            continue
        for action in element.get("actions", []):
            value = action.get("value") or {}
            if value.get("action") == "memory_approve":
                proposal_id = value.get("proposal_id")
    assert proposal_id

    listed_before = client.get(
        "/api/v1/projects/demo/payment/memories",
        headers=project_agent_headers(actor_id="u1", chat_id="chat-1", tenant_key="demo"),
    )
    assert listed_before.json() == []

    decide = client.post(
        "/api/v1/feishu/events",
        json=_card_action_payload(
            event_id="memory-approve-event",
            action="memory_approve",
            proposal_id=proposal_id,
        ),
    )
    assert decide.status_code == 200
    assert decide.json()["status"] == "accepted"
    assert decide.json()["proposal_id"] == proposal_id

    listed = client.get(
        "/api/v1/projects/demo/payment/memories",
        headers=project_agent_headers(actor_id="u1", chat_id="chat-1", tenant_key="demo"),
    )
    assert len(listed.json()) == 1
    assert listed.json()[0]["approved_by"] == "feishu-lead"

    decision_card = _last_card(app)
    assert "已沉淀到项目记忆" in str(decision_card)


def test_feishu_card_action_reject_does_not_write_memory() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="memory-reject-create", text=TRACEBACK),
    )
    _confirm_message(client, response=response, event_id="memory-reject-confirm")
    card = _last_card(app)
    proposal_id = None
    for element in card.get("elements", []):
        if element.get("tag") != "action":
            continue
        for action in element.get("actions", []):
            value = action.get("value") or {}
            if value.get("action") == "memory_reject":
                proposal_id = value.get("proposal_id")
    assert proposal_id

    decide = client.post(
        "/api/v1/feishu/events",
        json=_card_action_payload(
            event_id="memory-reject-event",
            action="memory_reject",
            proposal_id=proposal_id,
        ),
    )
    assert decide.status_code == 200
    assert client.get(
        "/api/v1/projects/demo/payment/memories",
        headers=project_agent_headers(actor_id="u1", chat_id="chat-1", tenant_key="demo"),
    ).json() == []
    assert "已拒绝沉淀" in str(_last_card(app))


def test_feishu_card_action_is_idempotent() -> None:
    app = create_app()
    _configure(app)
    client = TestClient(app)
    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="memory-idem-create", text=TRACEBACK),
    )
    _confirm_message(client, response=response, event_id="memory-idem-confirm")
    card = _last_card(app)
    proposal_id = None
    for element in card.get("elements", []):
        for action in element.get("actions", []):
            value = action.get("value") or {}
            if value.get("proposal_id"):
                proposal_id = value["proposal_id"]
                break
    assert proposal_id
    first = client.post(
        "/api/v1/feishu/events",
        json=_card_action_payload(
            event_id="memory-idem-event",
            action="memory_approve",
            proposal_id=proposal_id,
        ),
    )
    second = client.post(
        "/api/v1/feishu/events",
        json=_card_action_payload(
            event_id="memory-idem-event",
            action="memory_approve",
            proposal_id=proposal_id,
        ),
    )
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(client.get(
        "/api/v1/projects/demo/payment/memories",
        headers=project_agent_headers(actor_id="u1", chat_id="chat-1", tenant_key="demo"),
    ).json()) == 1
