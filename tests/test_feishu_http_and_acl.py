import asyncio
import json
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from project_lens.integrations.feishu.adapter import RecordingFeishuMessenger
from project_lens.integrations.feishu.http_adapter import (
    FeishuTenantTokenProvider,
    HttpFeishuMessenger,
)
from project_lens.domain.models import ProjectRef
from project_lens.integrations.feishu.identity import ConfigurableFeishuIdentityMapper, parse_project_bindings
from project_lens.main import create_app
from project_lens.project_space.models import ProjectSpace, RepositoryRef
from project_lens.project_space.policies import (
    AnswerDepth,
    ChatVisibilityPolicy,
    EffectiveAccessScope,
    ProjectMemberRolePolicy,
    ProjectRuntimeContextResolver,
    RoleKind,
    VisibilityLevel,
    effective_scope_to_audit_dict,
)
from project_lens.runtime.lifecycle import LifecycleEventType


LOCAL_TOKEN = "project-lens-local-token"


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict[str, object]]:
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if "tenant_access_token" in url:
            return 200, {"code": 0, "tenant_access_token": "tenant-token", "expire": 3600}
        return 200, {"code": 0, "data": {"message_id": "om_123"}}


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


def _configure_app(
    app,
    *,
    chat_id: str = "chat-1",
    space: ProjectSpace | None = None,
) -> ProjectRef:
    verifier = app.state.feishu_event_service._verifier
    verifier._signing_secret = None
    verifier._verification_token = LOCAL_TOKEN
    project = _project()
    app.state.feishu_event_service._identity_mapper = ConfigurableFeishuIdentityMapper(
        bindings={("demo", chat_id): project}
    )
    messenger = RecordingFeishuMessenger()
    app.state.feishu_messenger = messenger
    app.state.feishu_event_service._messenger = messenger
    if space is not None:
        app.state.project_registry._spaces[(space.tenant_id, space.project_id)] = space
        resolver = ProjectRuntimeContextResolver(
            project_registry=app.state.project_registry,
        )
        app.state.project_runtime_context_resolver = resolver
        app.state.feishu_event_service._runtime_context_resolver = resolver
    return project


def _message_payload(
    *,
    event_id: str,
    text: str,
    chat_id: str = "chat-1",
    user_id: str = "u_dev",
) -> dict[str, object]:
    return {
        "schema": "2.0",
        "token": LOCAL_TOKEN,
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": "demo",
        },
        "event": {
            "sender": {"sender_id": {"open_id": user_id}},
            "message": {
                "message_id": f"m-{event_id}",
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def _space_with_developer_and_chat_policy(
    *,
    chat_id: str = "chat-1",
    allowed_roles: tuple[RoleKind, ...] = (RoleKind.DEVELOPER, RoleKind.QA),
) -> ProjectSpace:
    project = _project()
    return ProjectSpace(
        tenant_id=project.tenant_id,
        project_id=project.project_id,
        display_name="Payment Demo",
        repositories=(RepositoryRef(name="payment", path=Path(".")),),
        services=(project.service,) if project.service else (),
        environments=(project.environment,) if project.environment else (),
        file_allowlist=("src/", "tests/", "knowledge/"),
        public_sources=("knowledge/",),
        role_policies=(
            ProjectMemberRolePolicy(
                actor_id="u_dev",
                project=project,
                role=RoleKind.DEVELOPER,
                readable_sources=("src/", "tests/"),
                allowed_tools=(
                    "search_context",
                    "read_project_file",
                    "query_graph",
                ),
                forbidden_sources=("secrets/",),
                answer_depth=AnswerDepth.DETAILED,
                answer_style="technical",
                visibility_level=VisibilityLevel.PRIVATE,
            ),
            ProjectMemberRolePolicy(
                actor_id="u_guest_product",
                project=project,
                role=RoleKind.PRODUCT,
                readable_sources=("knowledge/",),
                allowed_tools=("search_context",),
                answer_depth=AnswerDepth.BRIEF,
                answer_style="business",
                visibility_level=VisibilityLevel.TEAM_SHARED,
            ),
        ),
        chat_visibility_policies=(
            ChatVisibilityPolicy(
                chat_id=chat_id,
                project=project,
                visibility_level=VisibilityLevel.TEAM_SHARED,
                allowed_roles=allowed_roles,
                readable_sources=("src/",),
                allowed_tools=("search_context",),
                forbidden_sources=("docs/private/",),
                answer_depth=AnswerDepth.BALANCED,
            ),
        ),
    )


def test_feishu_http_adapter_caches_token_and_posts_card() -> None:
    from unittest.mock import patch

    transport = FakeTransport()
    provider = FeishuTenantTokenProvider(
        app_id="cli_test",
        app_secret="secret",
        base_url="https://example.test",
        transport=transport,
    )
    messenger = HttpFeishuMessenger(
        token_provider=provider,
        base_url="https://example.test",
        transport=transport,
    )

    with patch(
        "project_lens.integrations.feishu.http_adapter.assert_external_calls_allowed"
    ):
        asyncio.run(messenger.post_card("chat-1", {"header": {"title": "hello"}}))
        asyncio.run(messenger.post_text("chat-1", "progress"))

    token_calls = [call for call in transport.calls if "tenant_access_token" in call["url"]]
    message_calls = [call for call in transport.calls if "/im/v1/messages" in call["url"]]
    assert len(token_calls) == 1
    assert len(message_calls) == 2
    assert message_calls[0]["headers"]["Authorization"] == "Bearer tenant-token"
    card_body = json.loads(message_calls[0]["body"].decode("utf-8"))
    assert card_body["receive_id"] == "chat-1"
    assert card_body["msg_type"] == "interactive"
    text_body = json.loads(message_calls[1]["body"].decode("utf-8"))
    assert text_body["msg_type"] == "text"
    assert json.loads(text_body["content"])["text"] == "progress"


def test_feishu_status_exposes_agent_mode_for_grey_release() -> None:
    app = create_app()
    _configure_app(app)
    client = TestClient(app)

    response = client.get("/api/v1/integrations/feishu/status")

    assert response.status_code == 200
    body = response.json()
    assert body["outbound_mode"] in {"recording", "http"}
    assert body["agent_mode"] == "hermes"
    assert body["ask_agent_mode"] == "hermes"
    assert body["model_provider"]
    assert "model_live" in body
    assert "model_fallback_to_stub" in body
    assert "model_openai_api_key_configured" in body
    assert "hermes_tool_loop_enabled" in body
    assert "hermes_bridge_active" in body
    assert body["hermes_provider"]
    assert body["hermes_model"]
    assert "hermes_base_url_configured" in body
    assert "hermes_api_key_configured" in body
    assert "api_key" not in body
    assert "sk-" not in str(body).lower()


def test_unmapped_feishu_chat_is_rejected() -> None:
    app = create_app()
    verifier = app.state.feishu_event_service._verifier
    verifier._signing_secret = None
    verifier._verification_token = LOCAL_TOKEN
    project = _project()
    app.state.feishu_event_service._identity_mapper = ConfigurableFeishuIdentityMapper(
        bindings={("demo", "chat-1"): project}
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="unauthorized-event", text="status?", chat_id="not-bound"),
    )

    assert response.status_code == 403
    assert "not mapped" in response.json()["detail"]


def test_feishu_binding_keeps_lens_tenant_separate_from_feishu_tenant_key() -> None:
    project = _project()
    mapper = parse_project_bindings(
        json.dumps(
            {
                "bindings": [
                    {
                        "tenant_key": "feishu-tenant-1",
                        "chat_id": "oc_group",
                        "project_id": "payment",
                    }
                ]
            }
        ),
        default_project=project,
    )

    context = mapper.resolve(
        tenant_key="feishu-tenant-1",
        chat_id="oc_group",
        user_id="u1",
    )

    assert context.project.tenant_id == "demo"
    assert context.project.project_id == "payment"


def test_feishu_message_resolves_runtime_context_into_session_and_lifecycle() -> None:
    app = create_app()
    space = _space_with_developer_and_chat_policy()
    project = _configure_app(app, space=space)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="runtime-resolve", text="介绍一下这个项目"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    run_id = response.json()["run_id"]

    session = app.state.conversation_store.get_by_binding(
        tenant_id=project.tenant_id,
        chat_id="chat-1",
        user_id="u_dev",
        project=project,
    )
    assert session is not None
    access = session.task_scratchpad.get("runtime_access")
    assert access is not None
    assert access["tenant_id"] == "demo"
    assert access["project_id"] == "payment"
    assert access["role"] == "developer"
    assert access["visibility_level"] == "team_shared"
    assert access["chat_type"] == "group"
    assert access["allow_private_details"] is False
    assert access["policy_version"] == "v1"
    assert access["identity_source"] == "feishu_event"
    assert access["allowed_tools"] == ["search_context"]
    assert access["readable_sources"] == ["src/"]
    assert "project_space" not in access
    assert set(access) == {
        "tenant_id",
        "project_id",
        "actor_id",
        "chat_id",
        "role",
        "visibility_level",
        "answer_depth",
        "answer_style",
        "allowed_tools",
        "readable_sources",
        "forbidden_sources",
        "identity_source",
        "policy_version",
        "chat_type",
        "allow_private_details",
    }

    created = [
        event
        for event in app.state.lifecycle_bus.of_type(LifecycleEventType.RUN_CREATED)
        if str(event.run_id) == run_id
    ]
    assert created
    runtime = created[0].payload["runtime_access"]
    assert runtime["tenant_id"] == "demo"
    assert runtime["project_id"] == "payment"
    assert runtime["role"] == "developer"
    assert runtime["visibility_level"] == "team_shared"
    assert runtime["chat_type"] == "group"
    assert runtime["allowed_tools"] == ["search_context"]
    assert created[0].payload["entry_mode"] == "natural_project_question"


def test_feishu_debug_project_slash_is_marked_and_stripped_before_run() -> None:
    app = create_app()
    _configure_app(app)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="debug-slash-entry",
            text="/project introduce this project",
        ),
    )

    assert response.status_code == 200
    run = app.state.run_service.get(UUID(response.json()["run_id"]))
    assert run is not None
    assert run.question == "introduce this project"
    created = [
        event
        for event in app.state.lifecycle_bus.of_type(LifecycleEventType.RUN_CREATED)
        if str(event.run_id) == response.json()["run_id"]
    ]
    assert created
    assert created[0].payload["entry_mode"] == "debug_slash"


def test_feishu_natural_project_question_never_falls_back_to_read_agent() -> None:
    app = create_app()
    _configure_app(app)
    # Deliberately corrupt the legacy mode selector. Explicit Hermes runtime
    # metadata must still own the run and no legacy execute call is available.
    app.state.run_service._agent_mode = "read_agent"
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="natural-read-agent",
            text="order_service.py create_order coupon relation?",
        ),
    )

    assert response.status_code == 200
    run = app.state.run_service.get(UUID(response.json()["run_id"]))
    assert run is not None
    assert run.runtime == "hermes"
    assert run.entry_mode == "natural_project_question"
    created = [
        event
        for event in app.state.lifecycle_bus.of_type(LifecycleEventType.RUN_CREATED)
        if str(event.run_id) == response.json()["run_id"]
    ]
    assert created[0].payload["entry_mode"] == "natural_project_question"


def test_feishu_natural_project_question_uses_one_real_hermes_run() -> None:
    app = create_app()
    _configure_app(app)
    client = TestClient(app)

    from project_lens.integrations.feishu.hermes_loop_runner import (
        HermesLoopRunResult,
        HermesLoopToolCall,
    )
    from project_lens.integrations.feishu import hermes_tool_loop as hermes_mod
    from project_lens.domain.models import ProjectAnswer

    assert not hasattr(hermes_mod, "_plan")

    citation = {
        "id": "ev-auth-feishu",
        "kind": "code",
        "source_uri": "src/order_service.py",
        "summary": "authorized evidence",
    }

    class _FakeRunner:
        def run(self, *, question, project, user_id, chat_id):
            del user_id, chat_id
            # Differ from old rule planner (search + read for .py path questions).
            return HermesLoopRunResult(
                final_response=f"Fake Hermes answer for {project.project_id}: {question}",
                tool_calls=(
                    HermesLoopToolCall(
                        name="projectlens_authorized_evidence",
                        arguments={"limit": 5},
                        envelope={
                            "ok": True,
                            "tool_name": "projectlens_authorized_evidence",
                            "summary": "authorized inventory",
                            "citations": [citation],
                            "evidence_refs": [citation],
                            "unknowns": [],
                            "tool_result_id": "tr-feishu-1",
                        },
                    ),
                ),
                ok=True,
                verified_answer=ProjectAnswer(
                    project=project,
                    status="unknown",
                    business_summary="Evidence is insufficient.",
                    technical_summary="Evidence is insufficient.",
                    unknowns=("Need cited evidence.",),
                ),
            )

    bridge = app.state.feishu_event_service._hermes_tool_loop_bridge
    assert bridge is not None
    bridge._loop_runner = _FakeRunner()

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="hermes-tool-loop-feishu",
            text="order_service.py create_order coupon relation?",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["run_id"]
    run = app.state.run_service.get(UUID(body["run_id"]))
    assert run is not None
    assert run.runtime == "hermes"
    assert run.hermes_loop_id is not None
    assert bridge.last_tool_names == ("projectlens_authorized_evidence",)
    assert "projectlens_search_context" not in bridge.last_tool_names
    assert "projectlens_read_project_file" not in bridge.last_tool_names
    assert bridge.last_envelope is not None
    assert bridge.last_envelope["citations"]
    assert bridge.last_envelope["evidence_refs"]
    assert bridge.last_envelope["tool_calls"]
    assert bridge.last_envelope["audit_ref"]["allow_apply"] is False
    assert bridge.last_envelope["audit_ref"]["loop_id"]
    assert bridge.last_loop_id is not None
    loop_id = bridge.last_loop_id
    durable = app.state.event_sink.for_run(run.id)
    assert durable
    assert any(event.type.value == "tool_started" for event in durable)
    events_resp = client.get(f"/api/v1/project-agent/hermes-loops/{run.id}/events")
    assert events_resp.status_code == 200
    events_body = events_resp.json()
    assert events_body["ok"] is True
    assert events_body["run_id"] == str(run.id)
    assert events_body["allow_apply"] is False
    assert events_body["event_count"] >= 2
    # Session scratchpad should carry the latest hermes_tool_loop pointer.
    from project_lens.runtime.lifecycle import LifecycleEventType

    found_scratch = False
    for event in app.state.lifecycle_bus.of_type(LifecycleEventType.SESSION_SAVED):
        sid = (event.payload or {}).get("session_id")
        if not sid:
            continue
        session = app.state.conversation_store.get(UUID(str(sid)))
        if session is None:
            continue
        entry = (session.task_scratchpad or {}).get("hermes_tool_loop")
        if isinstance(entry, dict) and entry.get("loop_id") == str(loop_id):
            found_scratch = True
            break
    assert found_scratch
    assert app.state.feishu_messenger.messages[-1].message_type == "text"

def test_feishu_chat_policy_narrows_developer_tools_by_intersection() -> None:
    app = create_app()
    space = _space_with_developer_and_chat_policy()
    _configure_app(app, space=space)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="narrow-dev", text="项目地图"),
    )
    assert response.status_code == 200

    created = app.state.lifecycle_bus.of_type(LifecycleEventType.RUN_CREATED)
    access = created[-1].payload["runtime_access"]
    # Intersection, not union: role had 3 tools / 2 sources; chat keeps only search_context + src/
    assert access["allowed_tools"] == ["search_context"]
    assert access["readable_sources"] == ["src/"]
    assert set(access["forbidden_sources"]) == {"docs/private/", "secrets/"}
    assert "read_project_file" not in access["allowed_tools"]
    assert "tests/" not in access["readable_sources"]


def test_feishu_disallowed_role_in_chat_is_rejected() -> None:
    app = create_app()
    space = _space_with_developer_and_chat_policy(allowed_roles=(RoleKind.DEVELOPER, RoleKind.QA))
    _configure_app(app, space=space)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="role-denied",
            text="介绍一下这个项目",
            user_id="u_guest_product",
        ),
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "product" in detail
    assert "chat-1" in detail


def test_feishu_without_policies_uses_guest_public_sources_only() -> None:
    app = create_app()
    project = _configure_app(app)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="default-guest",
            text="介绍一下这个项目",
            user_id="u_unknown",
        ),
    )
    assert response.status_code == 200

    session = app.state.conversation_store.get_by_binding(
        tenant_id=project.tenant_id,
        chat_id="chat-1",
        user_id="u_unknown",
        project=project,
    )
    assert session is not None
    access = session.task_scratchpad["runtime_access"]
    assert access["tenant_id"] == "demo"
    assert access["project_id"] == "payment"
    assert access["role"] == "guest"
    assert access["visibility_level"] == "team_shared"
    assert access["answer_depth"] == "brief"
    assert access["readable_sources"] == ["knowledge/"]
    assert access["allowed_tools"] == ["search_context"]


def test_feishu_card_ask_question_also_records_runtime_access() -> None:
    app = create_app()
    space = _space_with_developer_and_chat_policy()
    project = _configure_app(app, space=space)
    client = TestClient(app)

    response = client.post(
        "/api/v1/feishu/events",
        json={
            "schema": "2.0",
            "token": LOCAL_TOKEN,
            "header": {
                "event_id": "card-ask-runtime",
                "event_type": "card.action.trigger",
                "tenant_key": "demo",
            },
            "event": {
                "operator": {"open_id": "u_dev"},
                "action": {
                    "tag": "button",
                    "value": {
                        "action": "ask_question",
                        "question": "介绍一下这个项目",
                    },
                },
                "context": {"open_chat_id": "chat-1"},
            },
        },
    )
    assert response.status_code == 200
    assert "run_id" in response.json()

    session = app.state.conversation_store.get_by_binding(
        tenant_id=project.tenant_id,
        chat_id="chat-1",
        user_id="u_dev",
        project=project,
    )
    assert session is not None
    assert session.task_scratchpad["runtime_access"]["role"] == "developer"
    created = app.state.lifecycle_bus.of_type(LifecycleEventType.RUN_CREATED)
    assert created[-1].payload["runtime_access"]["allowed_tools"] == ["search_context"]


def test_role_view_is_not_used_for_permission_calculation() -> None:
    """RoleView is presentation-only; ACL comes from EffectiveAccessScope intersection."""

    app = create_app()
    space = _space_with_developer_and_chat_policy()
    _configure_app(app, space=space)
    client = TestClient(app)

    # Audience-switch wording must not widen tools beyond chat∩role scope.
    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="roleview-not-acl",
            text="介绍一下这个项目，给我技术版详情",
        ),
    )
    assert response.status_code == 200
    access = app.state.lifecycle_bus.of_type(LifecycleEventType.RUN_CREATED)[-1].payload[
        "runtime_access"
    ]
    assert access["role"] == "developer"
    assert access["allowed_tools"] == ["search_context"]
    assert access["answer_depth"] == "balanced"

    # Disallowed role is rejected by resolver before any RoleView replay.
    denied = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(
            event_id="roleview-denied",
            text="切换到业务视图",
            user_id="u_guest_product",
        ),
    )
    assert denied.status_code == 403
    assert "product" in denied.json()["detail"]


def test_effective_scope_to_audit_dict_is_safe_summary() -> None:
    project = _project()
    scope = EffectiveAccessScope(
        project=project,
        actor_id="u_dev",
        chat_id="chat-1",
        role=RoleKind.DEVELOPER,
        readable_sources=("src/",),
        allowed_tools=("search_context",),
        forbidden_sources=("secrets/",),
        answer_depth=AnswerDepth.BALANCED,
        answer_style="technical",
        visibility_level=VisibilityLevel.PRIVATE,
    )
    payload = effective_scope_to_audit_dict(scope)
    assert payload["tenant_id"] == "demo"
    assert payload["project_id"] == "payment"
    assert payload["actor_id"] == "u_dev"
    assert payload["chat_id"] == "chat-1"
    assert "repositories" not in payload
    assert "evidence" not in payload
    assert "prompt" not in payload
