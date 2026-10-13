from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_lens.domain.models import ProjectRef
from project_lens.project_space.policies import (
    AnswerDepth,
    ProjectRuntimeContextResolver,
    RoleKind,
    VisibilityLevel,
)
from project_lens.project_space.registry import (
    ProjectRegistry,
    load_project_space_json,
)


def _project() -> ProjectRef:
    return ProjectRef(tenant_id="demo", project_id="payment", service="order-service")


def test_load_project_space_json_parses_role_and_chat_policies(tmp_path: Path) -> None:
    payload = {
        "tenant_id": "demo",
        "project_id": "payment",
        "display_name": "Payment Demo",
        "repositories": [{"name": "payment", "path": "examples/payment_service"}],
        "role_policies": [
            {
                "actor_id": "u_dev",
                "role": "developer",
                "readable_sources": ["src/", "tests/"],
                "allowed_tools": ["search_context", "read_project_file"],
                "forbidden_sources": ["secrets/"],
                "answer_depth": "detailed",
                "answer_style": "technical",
                "visibility_level": "private",
            }
        ],
        "chat_visibility_policies": [
            {
                "chat_id": "chat-1",
                "visibility_level": "team_shared",
                "allowed_roles": ["developer", "qa"],
                "readable_sources": ["src/"],
                "allowed_tools": ["search_context"],
            }
        ],
    }
    path = tmp_path / "payment.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    space = load_project_space_json(path, base_dir=tmp_path)

    assert space.role_policies[0].actor_id == "u_dev"
    assert space.role_policies[0].role == RoleKind.DEVELOPER
    assert space.role_policies[0].answer_depth == AnswerDepth.DETAILED
    assert space.role_policies[0].visibility_level == VisibilityLevel.PRIVATE
    assert space.chat_visibility_policies[0].chat_id == "chat-1"
    assert space.chat_visibility_policies[0].allowed_roles == (
        RoleKind.DEVELOPER,
        RoleKind.QA,
    )


def test_runtime_context_resolver_uses_role_and_chat_intersection(tmp_path: Path) -> None:
    project = _project()
    path = tmp_path / "project_space_with_policies.json"
    path.write_text(
        json.dumps(
            {
                "tenant_id": "demo",
                "project_id": "payment",
                "display_name": "Payment Demo",
                "repositories": [{"name": "payment", "path": "examples/payment_service"}],
                "services": ["order-service"],
                "role_policies": [
                    {
                        "actor_id": "u_dev",
                        "role": "developer",
                        "readable_sources": ["src/", "tests/"],
                        "allowed_tools": [
                            "search_context",
                            "read_project_file",
                            "authorized_evidence",
                        ],
                        "forbidden_sources": ["secrets/"],
                        "answer_depth": "detailed",
                        "answer_style": "technical",
                        "visibility_level": "private",
                    }
                ],
                "chat_visibility_policies": [
                    {
                        "chat_id": "chat-1",
                        "visibility_level": "team_shared",
                        "allowed_roles": ["developer", "qa"],
                        "readable_sources": ["src/"],
                        "allowed_tools": ["search_context"],
                        "forbidden_sources": ["docs/private/"],
                        "answer_depth": "balanced",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    space = load_project_space_json(path, base_dir=tmp_path)
    registry = ProjectRegistry((space,))
    resolver = ProjectRuntimeContextResolver(project_registry=registry)

    resolved = resolver.resolve(
        tenant_id="demo",
        project_id="payment",
        chat_id="chat-1",
        user_id="u_dev",
        chat_type="group",
    )

    assert resolved.project == project
    assert resolved.role_policy.actor_id == "u_dev"
    assert resolved.chat_policy.chat_id == "chat-1"
    assert resolved.effective_scope.project == project
    assert resolved.effective_scope.role == RoleKind.DEVELOPER
    assert resolved.effective_scope.readable_sources == ("src/",)
    assert resolved.effective_scope.allowed_tools == ("search_context",)
    assert resolved.effective_scope.forbidden_sources == ("docs/private/", "secrets/")
    assert resolved.effective_scope.answer_depth == AnswerDepth.BALANCED
    assert resolved.effective_scope.visibility_level == VisibilityLevel.TEAM_SHARED
    assert resolved.effective_scope.chat_type == "group"
    assert resolved.effective_scope.allow_private_details is False


def test_runtime_context_resolver_falls_back_to_guest_public_sources(tmp_path: Path) -> None:
    path = tmp_path / "project_space_without_policies.json"
    path.write_text(
        json.dumps(
            {
                "tenant_id": "demo",
                "project_id": "payment",
                "display_name": "Payment Demo",
                "repositories": [{"name": "payment", "path": "examples/payment_service"}],
                "public_sources": ["knowledge/"],
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
        chat_id="chat-9",
        user_id="u_unknown",
    )

    assert resolved.role_policy.role == RoleKind.GUEST
    assert resolved.chat_policy.visibility_level == VisibilityLevel.TEAM_SHARED
    assert resolved.effective_scope.readable_sources == ("knowledge/",)
    assert resolved.effective_scope.allowed_tools == ("search_context",)
    assert resolved.effective_scope.visibility_level == VisibilityLevel.TEAM_SHARED


def test_runtime_context_resolver_denies_unknown_without_public_sources(tmp_path: Path) -> None:
    path = tmp_path / "project_space_no_public.json"
    path.write_text(
        json.dumps(
            {
                "tenant_id": "demo",
                "project_id": "payment",
                "display_name": "Payment Demo",
                "repositories": [{"name": "payment", "path": "examples/payment_service"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    space = load_project_space_json(path, base_dir=tmp_path)
    resolver = ProjectRuntimeContextResolver(project_registry=ProjectRegistry((space,)))

    with pytest.raises(PermissionError, match="public project sources"):
        resolver.resolve(
            tenant_id="demo",
            project_id="payment",
            chat_id="chat-9",
            user_id="u_unknown",
        )


def test_effective_scope_uses_intersection_not_union() -> None:
    from project_lens.project_space.policies import (
        ChatVisibilityPolicy,
        ProjectMemberRolePolicy,
        combine_role_and_chat_policy,
    )

    project = _project()
    role = ProjectMemberRolePolicy(
        actor_id="u_dev",
        project=project,
        role=RoleKind.DEVELOPER,
        readable_sources=("src/", "tests/", "knowledge/"),
        allowed_tools=("search_context", "read_project_file", "authorized_evidence"),
    )
    chat = ChatVisibilityPolicy(
        chat_id="chat-1",
        project=project,
        readable_sources=("src/", "docs/"),
        allowed_tools=("search_context", "list_knowledge_gaps"),
    )
    scope = combine_role_and_chat_policy(
        role_policy=role,
        chat_policy=chat,
        chat_type="group",
    )
    assert scope.readable_sources == ("src/",)
    assert scope.allowed_tools == ("search_context",)
    assert "tests/" not in scope.readable_sources
    assert "docs/" not in scope.readable_sources
    assert "authorized_evidence" not in scope.allowed_tools
