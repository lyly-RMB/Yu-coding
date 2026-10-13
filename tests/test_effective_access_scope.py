"""EffectiveAccessScope fields and p2p vs group behavior."""

from __future__ import annotations

import json
from pathlib import Path

from project_lens.project_space.policies import (
    AnswerDepth,
    ProjectRuntimeContextResolver,
    VisibilityLevel,
    effective_scope_from_audit_dict,
    effective_scope_to_audit_dict,
)
from project_lens.project_space.registry import ProjectRegistry, load_project_space_json


def _developer_group_project(tmp_path: Path) -> Path:
    path = tmp_path / "payment.json"
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
                        "readable_sources": ["src/", "tests/", "knowledge/"],
                        "allowed_tools": [
                            "search_context",
                            "read_project_file",
                            "authorized_evidence",
                        ],
                        "answer_depth": "detailed",
                        "answer_style": "technical",
                        "visibility_level": "private",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_p2p_uses_full_role_scope_with_private_details(tmp_path: Path) -> None:
    space = load_project_space_json(_developer_group_project(tmp_path), base_dir=tmp_path)
    resolver = ProjectRuntimeContextResolver(project_registry=ProjectRegistry((space,)))

    resolved = resolver.resolve(
        tenant_id="demo",
        project_id="payment",
        chat_id="ou_p2p_user",
        user_id="u_dev",
        chat_type="p2p",
        identity_source="feishu_event",
    )

    scope = resolved.effective_scope
    assert scope.chat_type == "p2p"
    assert scope.readable_sources == ("src/", "tests/", "knowledge/")
    assert "read_project_file" in scope.allowed_tools
    assert scope.allow_private_details is True
    assert scope.visibility_level == VisibilityLevel.PRIVATE
    assert scope.identity_source == "feishu_event"
    assert scope.policy_version == "v1"

    audit = effective_scope_to_audit_dict(scope)
    assert audit["chat_type"] == "p2p"
    assert audit["allow_private_details"] is True
    assert audit["policy_version"] == "v1"
    assert "identity_source" in audit


def test_group_caps_private_visibility_and_tools(tmp_path: Path) -> None:
    space = load_project_space_json(_developer_group_project(tmp_path), base_dir=tmp_path)
    resolver = ProjectRuntimeContextResolver(project_registry=ProjectRegistry((space,)))

    resolved = resolver.resolve(
        tenant_id="demo",
        project_id="payment",
        chat_id="oc_payment",
        user_id="u_dev",
        chat_type="group",
    )

    scope = resolved.effective_scope
    assert scope.chat_type == "group"
    assert scope.readable_sources == ("src/", "tests/", "knowledge/")
    assert scope.allow_private_details is False
    assert scope.visibility_level == VisibilityLevel.TEAM_SHARED


def test_product_role_in_group_is_more_restricted_than_dev_p2p(tmp_path: Path) -> None:
    path = tmp_path / "payment_roles.json"
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
                "chat_visibility_policies": [
                    {
                        "chat_id": "oc_payment",
                        "chat_type": "group",
                        "readable_sources": ["knowledge/", "docs/"],
                        "allowed_tools": ["search_context", "list_knowledge_gaps"],
                        "answer_depth": "brief",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    space = load_project_space_json(path, base_dir=tmp_path)
    resolver = ProjectRuntimeContextResolver(project_registry=ProjectRegistry((space,)))

    dev_p2p = resolver.resolve(
        tenant_id="demo",
        project_id="payment",
        chat_id="ou_dev",
        user_id="u_dev",
        chat_type="p2p",
    )
    pm_group = resolver.resolve(
        tenant_id="demo",
        project_id="payment",
        chat_id="oc_payment",
        user_id="u_pm",
        chat_type="group",
    )

    assert "read_project_file" in dev_p2p.effective_scope.allowed_tools
    assert "src/" in dev_p2p.effective_scope.readable_sources
    assert "read_project_file" not in pm_group.effective_scope.allowed_tools
    assert pm_group.effective_scope.readable_sources == ("knowledge/",)
    assert pm_group.effective_scope.answer_depth == AnswerDepth.BRIEF


def test_runtime_scope_round_trip_is_bound_to_current_actor_and_chat(tmp_path: Path) -> None:
    space = load_project_space_json(_developer_group_project(tmp_path), base_dir=tmp_path)
    resolver = ProjectRuntimeContextResolver(project_registry=ProjectRegistry((space,)))
    resolved = resolver.resolve(
        tenant_id="demo",
        project_id="payment",
        chat_id="oc_payment",
        user_id="u_dev",
        chat_type="group",
        identity_source="feishu_event",
    )
    payload = effective_scope_to_audit_dict(resolved.effective_scope)

    restored = effective_scope_from_audit_dict(
        payload,
        project=resolved.project,
        actor_id="u_dev",
        chat_id="oc_payment",
    )
    assert restored == resolved.effective_scope

    try:
        effective_scope_from_audit_dict(
            payload,
            project=resolved.project,
            actor_id="u_pm",
            chat_id="oc_payment",
        )
    except ValueError as exc:
        assert "actor" in str(exc)
    else:
        raise AssertionError("scope snapshot must not be reusable by another actor")
