"""Virtual-member /sim turns must share the physical sender's conversation session."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

from fastapi.testclient import TestClient

from project_lens.domain.models import ProjectAnswer, ProjectRef
from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
from project_lens.integrations.feishu.hermes_tool_loop import FeishuHermesToolLoopResult
from project_lens.integrations.feishu.identity import parse_project_bindings
from project_lens.integrations.feishu.virtual_members import VirtualMemberController
from project_lens.main import create_app
from project_lens.project_space.policies import RoleKind

LOCAL_TOKEN = "project-lens-local-token"
CHAT_ID = "chat-1"


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


def _configure_projectlens(app) -> ProjectRef:  # noqa: ANN001
    verifier = app.state.feishu_event_service._verifier
    verifier._verification_token = LOCAL_TOKEN
    verifier._signing_secret = None
    project = ProjectRef(
        tenant_id="demo",
        project_id="projectlens",
        service="project-lens-api",
        environment="local",
    )
    app.state.feishu_event_service._identity_mapper = parse_project_bindings(
        "", default_project=project, allow_demo_fallback=True
    )
    app.state.feishu_messenger = RecordingFeishuMessenger()
    app.state.feishu_event_service._messenger = app.state.feishu_messenger
    app.state.feishu_hermes_tool_loop_bridge.answer = _FakeHermesBridge().answer
    app.state.feishu_event_service._context_preview_enabled = False
    app.state.feishu_event_service._virtual_members = VirtualMemberController(
        enabled=True,
        operators="u1",
    )
    # chat-1 fixture historically omitted product; allow it for multi-role /sim replay.
    space = app.state.project_registry.require("demo", "projectlens")
    updated_chats = tuple(
        replace(
            policy,
            allowed_roles=tuple(
                dict.fromkeys(
                    (
                        *policy.allowed_roles,
                        RoleKind.PRODUCT,
                        RoleKind.OPS,
                        RoleKind.MANAGER,
                    )
                )
            ),
        )
        if policy.chat_id == CHAT_ID
        else policy
        for policy in space.chat_visibility_policies
    )
    app.state.project_registry.upsert(replace(space, chat_visibility_policies=updated_chats))
    return project


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
                "chat_id": CHAT_ID,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_virtual_sim_roles_share_physical_sender_session() -> None:
    """PL-FEISHU-001: multi-role /sim then plain summarize stay on one session."""

    app = create_app()
    project = _configure_projectlens(app)
    client = TestClient(app)

    first = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="sim-product",
            text="/sim product payment retry should change from two to three",
        ),
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "accepted"
    first_session_id = first_body["session_id"]

    second = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="sim-developer",
            text="/sim developer order service hardcodes two retries; confirm interval",
        ),
    )
    assert second.status_code == 200, second.text
    assert second.json()["session_id"] == first_session_id

    third = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="sim-qa",
            text="/sim qa acceptance criteria miss the third retry case",
        ),
    )
    assert third.status_code == 200, third.text
    assert third.json()["session_id"] == first_session_id

    summary = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="plain-summary",
            text="Please summarize the discussion and list confirmed vs open items by role.",
        ),
    )
    assert summary.status_code == 200, summary.text
    summary_body = summary.json()
    assert summary_body["session_id"] == first_session_id

    session = app.state.conversation_store.get_by_binding(
        tenant_id="demo",
        chat_id=CHAT_ID,
        user_id="u1",
        project=project,
    )
    assert session is not None
    assert str(session.session_id) == first_session_id
    texts = [turn.text for turn in session.recent_turns]
    assert any("from two to three" in text for text in texts)
    assert any("hardcodes two retries" in text for text in texts)
    assert any("third retry case" in text for text in texts)
    assert any("summarize the discussion" in text for text in texts)
    assert {turn.user_id for turn in session.recent_turns} >= {
        "virtual-product",
        "virtual-developer",
        "virtual-qa",
    }

    for virtual_id in ("virtual-product", "virtual-developer", "virtual-qa"):
        leaked = app.state.conversation_store.get_by_binding(
            tenant_id="demo",
            chat_id=CHAT_ID,
            user_id=virtual_id,
            project=project,
        )
        assert leaked is None, virtual_id

    run = app.state.run_service.get(UUID(summary_body["run_id"]))
    assert run is not None
    assert "summarize the discussion" in run.question
