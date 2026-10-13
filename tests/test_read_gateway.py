"""ReadContextGateway: ACL + audit facade over ContextEngine read tools."""

from __future__ import annotations

import pytest

from project_lens.context.models import AccessContext, ContextQuery
from project_lens.domain.models import ProjectRef
from project_lens.main import create_app
from project_lens.project_space.policies import (
    EffectiveAccessScope,
    RoleKind,
    VisibilityLevel,
    AnswerDepth,
)
from project_lens.runtime.policy import RiskClass
from project_lens.runtime.read_gateway import ReadContextGateway
from project_lens.runtime.tool_specs import ToolLane, assert_tool_callable


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


def _access() -> AccessContext:
    return AccessContext(
        tenant_id="demo",
        user_id="u1",
        permissions=frozenset({"project:payment:read"}),
    )


def _narrow_scope() -> EffectiveAccessScope:
    project = _project()
    return EffectiveAccessScope(
        project=project,
        actor_id="u1",
        chat_id="oc_payment",
        role=RoleKind.GUEST,
        readable_sources=("knowledge/",),
        allowed_tools=("search_context",),
        forbidden_sources=(),
        answer_depth=AnswerDepth.BRIEF,
        answer_style="team",
        visibility_level=VisibilityLevel.TEAM_SHARED,
        chat_type="group",
        allow_private_details=False,
    )


def test_read_gateway_enforces_scope_before_search() -> None:
    app = create_app()
    gateway = ReadContextGateway(app.state.context_engine)
    scope = _narrow_scope()
    scope_blocked = EffectiveAccessScope(
        project=scope.project,
        actor_id=scope.actor_id,
        chat_id=scope.chat_id,
        role=scope.role,
        readable_sources=scope.readable_sources,
        allowed_tools=(),
        forbidden_sources=scope.forbidden_sources,
        answer_depth=scope.answer_depth,
        answer_style=scope.answer_style,
        visibility_level=scope.visibility_level,
        identity_source=scope.identity_source,
        policy_version=scope.policy_version,
        chat_type=scope.chat_type,
        allow_private_details=scope.allow_private_details,
    )

    with pytest.raises(PermissionError, match="search_context"):
        gateway.search_context(
            ContextQuery(text="coupon", project=_project(), limit=3),
            _access(),
            scope=scope_blocked,
        )

    bundle = gateway.search_context(
        ContextQuery(text="coupon architecture", project=_project(), limit=20),
        _access(),
        scope=scope,
    )
    assert bundle.hits
    assert all(item.type.value != "code" for item in bundle.evidence)
    event = gateway.audit_events[-1]
    assert event.arguments.get("actor_id") == "u1"
    assert event.arguments.get("policy_version") == "v1"
    assert event.arguments.get("chat_type") == "group"


def test_read_gateway_refuses_allow_apply() -> None:
    app = create_app()
    with pytest.raises(ValueError, match="allow_apply"):
        ReadContextGateway(app.state.context_engine, allow_apply=True)


def test_strict_read_gateway_rejects_missing_scope_before_search() -> None:
    app = create_app()
    gateway = ReadContextGateway(app.state.context_engine, require_scope=True)

    with pytest.raises(PermissionError, match="effective access scope"):
        gateway.search_context(
            ContextQuery(text="coupon", project=_project(), limit=3),
            _access(),
        )

    assert gateway.audit_events == []


def test_read_gateway_audits_search_and_never_apply() -> None:
    app = create_app()
    gateway = ReadContextGateway(app.state.context_engine)
    bundle = gateway.search_context(
        ContextQuery(text="coupon AttributeError", project=_project(), limit=5),
        _access(),
    )
    assert bundle.hits
    names = [event.tool_name for event in gateway.audit_events]
    assert names == ["search_context"]
    assert all(event.risk_class == RiskClass.READ for event in gateway.audit_events)
    assert gateway.audit_summary()["allow_apply"] is False
    assert gateway.audit_summary()["apply_events"] == []


def test_read_gateway_authorized_and_gaps() -> None:
    app = create_app()
    gateway = ReadContextGateway(app.state.context_engine)
    project = _project()
    access = _access()
    gaps = gateway.list_knowledge_gaps(project, access)
    evidence = gateway.authorized_evidence(project, access, limit=20)
    assert gaps.gaps is not None
    assert evidence
    names = {event.tool_name for event in gateway.audit_events}
    assert {
        "list_knowledge_gaps",
        "authorized_evidence",
    } <= names
    assert_tool_callable("authorized_evidence", allow_apply=False)
    assert all(spec.category == ToolLane.READ for spec in gateway.list_read_specs())
