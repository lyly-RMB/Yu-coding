"""Pytest isolation for ProjectLens Phase 0.

Neutralize developer `.env` leakage into the global Settings singleton before
tests import create_app / external clients.
"""

from __future__ import annotations


def pytest_configure(config) -> None:  # noqa: ANN001
    from project_lens import config as app_config

    app_config.settings.test_mode = True
    app_config.settings.model_live = False
    app_config.settings.allow_external_calls = False
    app_config.settings.agent_mode = "hermes"
    app_config.settings.obsidian_enabled = False
    # Keep preview card path as the default under pytest; opt out per-test.
    app_config.settings.feishu_context_preview_enabled = True


def project_agent_headers(
    *,
    actor_id: str = "u1",
    chat_id: str = "chat-1",
    tenant_key: str = "demo",
    chat_type: str = "group",
) -> dict[str, str]:
    """Trusted test identity for project-agent routes under isolation."""

    return {
        "X-ProjectLens-Actor-Id": actor_id,
        "X-ProjectLens-Chat-Id": chat_id,
        "X-ProjectLens-Tenant-Key": tenant_key,
        "X-ProjectLens-Chat-Type": chat_type,
    }


def runtime_access_for_agent(
    agent,
    *,
    project,
    user_id: str,
    chat_id: str,
    chat_type: str = "group",
    identity_source: str = "test_fixture",
) -> dict[str, object]:
    """Resolve the same audit-safe scope snapshot production adapters persist."""

    from project_lens.project_space.policies import (
        AnswerDepth,
        DEFAULT_READ_TOOLS,
        EffectiveAccessScope,
        ProjectRuntimeContextResolver,
        RoleKind,
        VisibilityLevel,
        effective_scope_to_audit_dict,
    )

    resolver = ProjectRuntimeContextResolver(project_registry=agent._registry)
    try:
        resolved = resolver.resolve(
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            chat_id=chat_id,
            user_id=user_id,
            chat_type=chat_type,
            identity_source=identity_source,
        )
        scope = resolved.effective_scope
    except PermissionError:
        space = agent._registry.require(project.tenant_id, project.project_id)
        scope = EffectiveAccessScope(
            project=project,
            actor_id=user_id,
            chat_id=chat_id,
            role=RoleKind.DEVELOPER,
            readable_sources=space.file_allowlist or ("knowledge/",),
            allowed_tools=DEFAULT_READ_TOOLS,
            forbidden_sources=(),
            answer_depth=AnswerDepth.DETAILED,
            answer_style="technical",
            visibility_level=VisibilityLevel.TEAM_SHARED,
            identity_source=identity_source,
            chat_type=chat_type,
            allow_private_details=chat_type == "p2p",
        )
    return effective_scope_to_audit_dict(scope)
