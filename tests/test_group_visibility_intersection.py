"""Group chat visibility intersection and speaker scope isolation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from project_lens.application.message_access_context import MessageAccessContext
from project_lens.context.models import AccessContext, ContextQuery
from project_lens.domain.identity import ActorContext
from project_lens.domain.models import ProjectRef
from project_lens.main import create_app
from project_lens.project_space.policies import (
    ChatVisibilityPolicy,
    ProjectMemberRolePolicy,
    ProjectRuntimeContextResolver,
    RoleKind,
    combine_role_and_chat_policy,
)
from project_lens.project_space.registry import ProjectRegistry, load_project_space_json
from project_lens.runtime.read_gateway import ReadContextGateway
from tests.conftest import project_agent_headers


def test_group_intersection_never_expands_role_scope() -> None:
    project = ProjectRef(tenant_id="demo", project_id="payment", service="order-service")
    role = ProjectMemberRolePolicy(
        actor_id="u_dev",
        project=project,
        role=RoleKind.DEVELOPER,
        readable_sources=("knowledge/",),
        allowed_tools=("search_context",),
    )
    chat = ChatVisibilityPolicy(
        chat_id="oc_payment",
        project=project,
        chat_type="group",
        readable_sources=("src/", "knowledge/", "docs/"),
        allowed_tools=("search_context", "read_project_file", "authorized_evidence"),
    )
    scope = combine_role_and_chat_policy(
        role_policy=role,
        chat_policy=chat,
        chat_type="group",
    )
    assert scope.readable_sources == ("knowledge/",)
    assert scope.allowed_tools == ("search_context",)
    assert "src/" not in scope.readable_sources


def test_high_privilege_user_cannot_expand_group_visibility(tmp_path: Path) -> None:
    path = tmp_path / "payment.json"
    path.write_text(
        json.dumps(
            {
                "tenant_id": "demo",
                "project_id": "payment",
                "display_name": "Payment Demo",
                "repositories": [{"name": "payment", "path": "examples/payment_service"}],
                "members": [{"actor_id": "u_dev", "roles": ["developer"]}],
                "chat_visibility_policies": [
                    {
                        "chat_id": "oc_payment",
                        "chat_type": "group",
                        "readable_sources": ["knowledge/"],
                        "allowed_tools": ["search_context"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    space = load_project_space_json(path, base_dir=tmp_path)
    resolver = ProjectRuntimeContextResolver(project_registry=ProjectRegistry((space,)))

    resolved = resolver.resolve(
        tenant_id="demo",
        project_id="payment",
        chat_id="oc_payment",
        user_id="u_dev",
        chat_type="group",
    )
    assert resolved.effective_scope.readable_sources == ("knowledge/",)
    assert resolved.effective_scope.allowed_tools == ("search_context",)


def test_message_access_context_is_per_actor_not_session_cached(tmp_path: Path) -> None:
    path = tmp_path / "payment.json"
    path.write_text(
        json.dumps(
            {
                "tenant_id": "demo",
                "project_id": "payment",
                "display_name": "Payment Demo",
                "repositories": [{"name": "payment", "path": "examples/payment_service"}],
                "members": [
                    {"actor_id": "u_dev", "roles": ["developer"]},
                    {"actor_id": "u_pm", "roles": ["product"]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    space = load_project_space_json(path, base_dir=tmp_path)
    registry = ProjectRegistry((space,))
    resolver = ProjectRuntimeContextResolver(project_registry=registry)

    dev_actor = ActorContext(
        tenant_key="demo",
        actor_id="u_dev",
        chat_id="oc_payment",
        chat_type="group",
        source="feishu_event",
        authenticated=True,
    )
    pm_actor = ActorContext(
        tenant_key="demo",
        actor_id="u_pm",
        chat_id="oc_payment",
        chat_type="group",
        source="feishu_event",
        authenticated=True,
    )

    dev_ctx = MessageAccessContext.resolve_for_actor(
        actor=dev_actor,
        project_id="payment",
        resolver=resolver,
        message_id="msg-1",
    )
    pm_ctx = MessageAccessContext.resolve_for_actor(
        actor=pm_actor,
        project_id="payment",
        resolver=resolver,
        message_id="msg-2",
    )

    assert dev_ctx.effective_scope.role == RoleKind.DEVELOPER
    assert pm_ctx.effective_scope.role == RoleKind.PRODUCT
    assert dev_ctx.effective_scope.answer_style != pm_ctx.effective_scope.answer_style
    assert "read_project_file" in dev_ctx.effective_scope.allowed_tools
    assert "read_project_file" not in pm_ctx.effective_scope.allowed_tools


def test_tool_api_audit_includes_policy_version_and_chat_type() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/project-agent/tools/call",
        json={
            "tool_name": "projectlens_search_context",
            "project": {
                "tenant_id": "demo",
                "project_id": "payment",
                "service": "order-service",
            },
            "user_id": "u1",
            "chat_id": "oc_payment",
            "arguments": {"query": "coupon", "limit": 2},
        },
        headers=project_agent_headers(
            actor_id="u1",
            chat_id="oc_payment",
            chat_type="group",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    visibility = body["visibility_scope"]
    assert visibility["actor_id"] == "u1"
    assert visibility["chat_type"] == "group"
    assert visibility["policy_version"] == "v1"
    assert visibility["role"] == "developer"


def test_group_cannot_read_src_via_read_project_file(tmp_path: Path) -> None:
    path = tmp_path / "payment.json"
    path.write_text(
        json.dumps(
            {
                "tenant_id": "demo",
                "project_id": "payment",
                "display_name": "Payment Demo",
                "repositories": [{"name": "payment", "path": "examples/payment_service"}],
                "members": [{"actor_id": "u_dev", "roles": ["developer"]}],
                "chat_visibility_policies": [
                    {
                        "chat_id": "oc_payment",
                        "chat_type": "group",
                        "readable_sources": ["knowledge/"],
                        "allowed_tools": [
                            "search_context",
                            "read_project_file",
                            "authorized_evidence",
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    space = load_project_space_json(path, base_dir=tmp_path)
    app = create_app()
    app.state.project_registry.upsert(space)
    client = TestClient(app)

    response = client.post(
        "/api/v1/project-agent/tools/call",
        json={
            "tool_name": "projectlens_read_project_file",
            "project": {
                "tenant_id": "demo",
                "project_id": "payment",
                "service": "order-service",
            },
            "user_id": "u_dev",
            "chat_id": "oc_payment",
            "arguments": {"path": "src/order_service.py"},
        },
        headers=project_agent_headers(
            actor_id="u_dev",
            chat_id="oc_payment",
            chat_type="group",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] in {"ACCESS_DENIED", "TOOL_EXECUTION_FAILED"}
    assert "access_denied" in body or "readable" in body["message"].casefold()


def test_group_search_filters_code_before_returning_evidence() -> None:
    app = create_app()
    resolver = ProjectRuntimeContextResolver(project_registry=app.state.project_registry)
    scope = resolver.resolve(
        tenant_id="demo",
        project_id="payment",
        chat_id="oc_knowledge_only",
        user_id="u1",
        chat_type="group",
    ).effective_scope
    scope = replace(
        scope,
        readable_sources=("knowledge/",),
        allowed_tools=("search_context",),
    )
    gateway = ReadContextGateway(app.state.context_engine, require_scope=True)
    bundle = gateway.search_context(
        ContextQuery(
            text="create_order coupon order_service.py",
            project=ProjectRef(tenant_id="demo", project_id="payment"),
            limit=20,
        ),
        AccessContext(
            tenant_id="demo",
            user_id="u1",
            permissions=frozenset({"project:payment:read"}),
        ),
        scope=scope,
    )

    assert all(item.type.value != "code" for item in bundle.evidence)
    assert int(bundle.retrieval_trace.get("scope_hidden_count") or 0) >= 1
