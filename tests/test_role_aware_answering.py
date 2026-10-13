"""Phase 3 role-aware projection over one verified fact core."""

from __future__ import annotations

from datetime import datetime, timezone

from project_lens.application.audience_views import (
    render_audience_markdown,
    render_audience_view,
)
from project_lens.domain.models import (
    ActionProposal,
    Claim,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceRef,
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
)


def _project() -> ProjectRef:
    return ProjectRef(tenant_id="demo", project_id="payment")


def _answer() -> ProjectAnswer:
    project = _project()
    evidence = (
        Evidence(
            type=EvidenceType.DOCUMENT,
            project=project,
            source=SourceRef(system="feishu_task", source_id="RQ-204"),
            content="RQ-204 expands checkout scope and is due 2026-09-02. Owner Ada.",
            observed_at=datetime.now(timezone.utc),
            access_scope="project:payment:read",
            content_hash="1234567890abcdefaa",
        ),
        Evidence(
            type=EvidenceType.CODE,
            project=project,
            source=SourceRef(system="repository", source_id="src/order_service.py"),
            content="module summary only; no source body is exposed by AudienceView",
            observed_at=datetime.now(timezone.utc),
            access_scope="project:payment:read",
            content_hash="2234567890abcdefaa",
            metadata={"path": "src/order_service.py"},
        ),
        Evidence(
            type=EvidenceType.DOCUMENT,
            project=project,
            source=SourceRef(system="ci", source_id="payment-regression"),
            content="CI regression is failing for checkout without coupon.",
            observed_at=datetime.now(timezone.utc),
            access_scope="project:payment:read",
            content_hash="3234567890abcdefaa",
        ),
    )
    facts = (
        Claim(
            text="RQ-204 扩大了结算需求范围，会影响未选择优惠券的用户路径。",
            type=ClaimType.FACT,
            evidence_ids=(evidence[0].id,),
            grade=EvidenceGrade.B,
        ),
        Claim(
            text="技术影响位于 src/order_service.py 的 create_order 模块。",
            type=ClaimType.FACT,
            evidence_ids=(evidence[1].id,),
            grade=EvidenceGrade.B,
        ),
        Claim(
            text="CI 回归用例 payment-regression 当前失败。",
            type=ClaimType.FACT,
            evidence_ids=(evidence[2].id,),
            grade=EvidenceGrade.B,
        ),
        Claim(
            text="负责人是 Ada，截止时间是 2026-09-02。",
            type=ClaimType.FACT,
            evidence_ids=(evidence[0].id,),
            grade=EvidenceGrade.B,
        ),
    )
    inference = Claim(
        text="如果失败用例未关闭，里程碑存在延期风险。",
        type=ClaimType.INFERENCE,
        evidence_ids=(),
        grade=EvidenceGrade.C,
    )
    citations = tuple(
        EvidenceRef(
            id=item.id,
            kind=item.type,
            source_uri=f"{item.source.system}:{item.source.source_id}",
            summary="verified source summary",
        )
        for item in evidence
    )
    return ProjectAnswer(
        project=project,
        status="identified",
        skill="project_investigation",
        confidence=0.82,
        business_summary="RQ-204 影响结算用户路径，当前 CI 失败形成交付风险。",
        technical_summary="Verified fact core for RQ-204.",
        conclusion="RQ-204 已影响结算范围，需在截止日前关闭回归失败。",
        claims=(*facts, inference),
        facts=facts,
        inferences=(inference,),
        evidence=evidence,
        impact=("结算用户路径受影响。", "当前回归失败会影响 2026-09-02 里程碑。"),
        next_actions=("Ada 确认需求范围。", "QA 关闭 payment-regression 失败用例。"),
        citations=citations,
        policy_used="v1:group",
        unknowns=("尚未确认失败用例是否阻塞正式发布。",),
        recommended_actions=(
            ActionProposal(title="Ada 确认需求范围。", requires_approval=True),
        ),
    )


def _scope(role: RoleKind) -> EffectiveAccessScope:
    return EffectiveAccessScope(
        project=_project(),
        actor_id=f"u_{role.value}",
        chat_id="oc_rq204",
        role=role,
        readable_sources=("knowledge/", "src/", "tests/"),
        allowed_tools=("search_context", "read_project_file", "authorized_evidence"),
        forbidden_sources=(),
        answer_depth=AnswerDepth.BALANCED,
        answer_style=role.value,
        visibility_level=VisibilityLevel.TEAM_SHARED,
        chat_type="group",
    )


def test_role_views_have_distinct_first_screen_priorities() -> None:
    answer = _answer()
    expected = {
        RoleKind.DEVELOPER: ("技术视图", "相关模块 / 文件 / 接口"),
        RoleKind.PRODUCT: ("业务/产品视图", "需求范围 / 当前进度"),
        RoleKind.QA: ("测试视图", "影响场景 / 回归范围"),
        RoleKind.MANAGER: ("管理/进度视图", "里程碑 / 截止时间"),
        RoleKind.OPS: ("运维视图", "时间线 / 最近变更"),
    }

    for role, (title, section) in expected.items():
        view = render_audience_view(
            answer,
            role=role,
            chat_type="group",
            scope=_scope(role),
        )
        markdown = render_audience_markdown(view)
        assert title in markdown
        assert section in markdown
        assert view.unknowns == answer.unknowns


def test_audience_renderer_cannot_add_fact_or_evidence() -> None:
    answer = _answer()
    allowed_text = {
        answer.conclusion,
        *(item.text for item in answer.facts),
        *answer.impact,
        *answer.unknowns,
        *answer.next_actions,
    }
    fact_ids = tuple(item.id for item in answer.facts)
    citation_ids = tuple(item.id for item in answer.citations)

    for role in (RoleKind.DEVELOPER, RoleKind.PRODUCT, RoleKind.QA, RoleKind.MANAGER):
        view = render_audience_view(
            answer,
            role=role,
            chat_type="group",
            scope=_scope(role),
        )
        assert tuple(item.id for item in view.facts) == fact_ids
        assert tuple(item.id for item in view.citations) == citation_ids
        assert view.unknowns == answer.unknowns
        for section in view.sections:
            assert set(section.items).issubset(allowed_text)


def test_product_view_does_not_render_source_body() -> None:
    answer = _answer()
    view = render_audience_view(
        answer,
        role=RoleKind.PRODUCT,
        chat_type="group",
        scope=_scope(RoleKind.PRODUCT),
    )
    markdown = render_audience_markdown(view)
    assert "module summary only" not in markdown
    assert "def create_order" not in markdown
    assert "需求范围 / 当前进度" in markdown

