from datetime import datetime, timezone
from uuid import UUID

from project_lens.application.answer_envelope import project_answer_to_envelope
from project_lens.domain.models import (
    AgentRun,
    Claim,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceType,
    ProjectAnswer,
    ProjectRef,
    SourceRef,
)
from project_lens.project_space.policies import (
    AnswerDepth,
    EffectiveAccessScope,
    RoleKind,
    VisibilityLevel,
    effective_scope_to_audit_dict,
)


def _envelope(role: RoleKind) -> dict[str, object]:
    project = ProjectRef(tenant_id="demo", project_id="payment")
    now = datetime.now(timezone.utc)
    doc = Evidence(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        type=EvidenceType.DOCUMENT,
        project=project,
        source=SourceRef(system="feishu", source_id="req-204"),
        content="RQ-204 expands checkout scope; owner Ada.",
        observed_at=now,
        access_scope="project:payment:read",
        content_hash="a" * 16,
    )
    code = Evidence(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        type=EvidenceType.CODE,
        project=project,
        source=SourceRef(system="github", source_id="src/order_service.py"),
        content="def create_order(): secret implementation body",
        observed_at=now,
        access_scope="project:payment:read",
        content_hash="b" * 16,
    )
    claims = (
        Claim(
            text="RQ-204 expands checkout scope.",
            type=ClaimType.FACT,
            evidence_ids=(doc.id,),
            grade=EvidenceGrade.B,
        ),
        Claim(
            text="Implementation touches src/order_service.py.",
            type=ClaimType.FACT,
            evidence_ids=(code.id,),
            grade=EvidenceGrade.B,
        ),
    )
    answer = ProjectAnswer(
        project=project,
        status="identified",
        skill="project_investigation",
        business_summary="RQ-204 affects checkout.",
        conclusion="RQ-204 affects checkout.",
        claims=claims,
        facts=claims,
        evidence=(doc, code),
    )
    scope = EffectiveAccessScope(
        project=project,
        actor_id=f"u-{role.value}",
        chat_id="oc-payment",
        role=role,
        readable_sources=("knowledge/", "src/"),
        allowed_tools=("search_context", "read_project_file"),
        forbidden_sources=(),
        answer_depth=AnswerDepth.BALANCED,
        answer_style=role.value,
        visibility_level=VisibilityLevel.TEAM_SHARED,
        identity_source="feishu_event",
        chat_type="group",
    )
    run = AgentRun(
        project=project,
        user_id=scope.actor_id,
        channel_id=scope.chat_id,
        question="当前进度如何？",
        runtime_access=effective_scope_to_audit_dict(scope),
    )
    return project_answer_to_envelope(run=run, answer=answer)


def test_same_group_question_shares_fact_and_evidence_ids_across_roles() -> None:
    views = [_envelope(role) for role in (RoleKind.DEVELOPER, RoleKind.PRODUCT, RoleKind.QA, RoleKind.MANAGER)]
    fact_ids = [[item["citations"] for item in view["facts"]] for view in views]
    assert all(item == fact_ids[0] for item in fact_ids)
    assert len({view["audience_view"]["title"] for view in views}) == 4


def test_group_non_developer_roles_receive_redacted_code_citation() -> None:
    for role in (RoleKind.PRODUCT, RoleKind.QA, RoleKind.MANAGER):
        citations = _envelope(role)["citations"]
        code = next(item for item in citations if item["kind"] == "file")
        assert "secret implementation body" not in code["summary"]
        assert "src/order_service.py" in code["source_uri"]
