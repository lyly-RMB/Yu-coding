"""Feishu AnswerView 2.0 / RoleView: audience-specific cards from the same ProjectAnswer."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

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
from project_lens.integrations.feishu.audiences import (
    AnswerAudience,
    detect_audience_switch,
    select_answer_audience,
)
from project_lens.integrations.feishu.audit_summary import FeishuAuditSummary
from project_lens.integrations.feishu.cards import render_answer_card


def _project() -> ProjectRef:
    return ProjectRef(
        tenant_id="demo",
        project_id="payment",
        service="order-service",
        environment="production",
    )


def _run(question: str) -> AgentRun:
    return AgentRun(
        project=_project(),
        user_id="u1",
        channel_id="chat-1",
        question=question,
    )


def _evidence(*, path: str, **meta: object) -> Evidence:
    return Evidence(
        id=uuid4(),
        type=EvidenceType.CODE,
        project=_project(),
        source=SourceRef(system="repository", source_id=path),
        content=f"content for {path}",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="1234567890abcdefaa",
        metadata={"path": path, **meta},
    )


def _rich_answer() -> ProjectAnswer:
    evid = (
        _evidence(path="src/order_service.py", function="create_order"),
        _evidence(path="knowledge/architecture.md"),
        _evidence(path="docs/runbook.md"),
        _evidence(path="tests/test_order_service.py"),
    )
    claims = (
        Claim(
            text="create_order 在 src/order_service.py 校验 coupon 后创建订单。",
            type=ClaimType.FACT,
            evidence_ids=(evid[0].id,),
            grade=EvidenceGrade.B,
        ),
        Claim(
            text="该入口影响支付下单用户路径。",
            type=ClaimType.FACT,
            evidence_ids=(evid[1].id,),
            grade=EvidenceGrade.B,
        ),
        Claim(
            text="可能与优惠券服务超时相关（推断）。",
            type=ClaimType.INFERENCE,
            evidence_ids=(),
            grade=EvidenceGrade.C,
        ),
    )
    return ProjectAnswer(
        project=_project(),
        status="identified",
        skill="project_investigation",
        confidence=0.72,
        business_summary="下单前校验 coupon，避免无效优惠进入支付链路。",
        technical_summary=(
            "tools=search_context,read_project_file; skill_guide=free_question; "
            "provider=stub_planner; citations=2"
        ),
        claims=claims,
        evidence=evid,
        unknowns=("优惠券服务超时阈值缺少近期监控证据。",),
    )


def _joined(card: dict[str, object]) -> str:
    return "\n".join(
        str(item.get("content") or "")
        for item in card.get("elements", [])  # type: ignore[union-attr]
        if isinstance(item, dict)
    )


def _first_screen(card: dict[str, object], *, n: int = 7) -> str:
    contents = [
        str(item.get("content") or "")
        for item in card.get("elements", [])  # type: ignore[union-attr]
        if isinstance(item, dict) and item.get("content")
    ]
    return "\n".join(contents[:n])


def test_detect_audience_switch_for_role_buttons() -> None:
    assert detect_audience_switch("给技术看的版本") == AnswerAudience.TECHNICAL
    assert detect_audience_switch("给产品/业务看的版本") == AnswerAudience.BUSINESS
    assert detect_audience_switch("给测试看的版本") == AnswerAudience.QA
    assert detect_audience_switch("查看证据") == AnswerAudience.EVIDENCE
    assert detect_audience_switch("回到团队视图") == AnswerAudience.TEAM
    # Long project questions must not be treated as RoleView switches.
    assert (
        detect_audience_switch("请从技术视角解释 create_order 为什么检查 coupon")
        is None
    )
    assert select_answer_audience("随便问一句") == AnswerAudience.TEAM


def test_default_team_card_not_flooded_by_sources_or_audit() -> None:
    answer = _rich_answer()
    audit = FeishuAuditSummary(
        skill="project_investigation",
        agent_mode="read_agent",
        provider="stub_planner",
        skill_guide="free_question",
        read_tool_names="search_context, read_project_file",
        evidence_ids_preview=", ".join(str(item.id) for item in answer.evidence[:3]),
        technical_summary_preview=answer.technical_summary,
        trace_id=str(uuid4()),
        allow_apply="False",
    )
    card = render_answer_card(
        _run("order_service.py 里 create_order 为什么要检查 coupon？"),
        answer,
        audit_summary=audit,
    )
    first = _first_screen(card)
    joined = _joined(card)

    assert "**回答**" in first
    assert "**当前状态**" in first
    assert "**对团队意味着什么**" in first or "**已确认**" in first
    assert "**来源摘要**" in joined
    assert "引用资料：" in joined

    # First screen must not be drowned by evidence list / SkillGuide / tools / provider.
    assert "**本次参考来源**" not in first
    assert "SkillGuide" not in first
    assert "free_question" not in first
    assert "stub_planner" not in first
    assert "search_context" not in first
    assert "tools=search_context" not in first
    assert "provider" not in first.casefold() or "provider=" not in first

    for item in answer.evidence:
        assert str(item.id) not in first

    # Debug / audit after the answer body.
    assert "**调试信息**" in joined
    assert joined.index("**回答**") < joined.index("**调试信息**")
    debug = joined[joined.index("**调试信息**") :]
    assert "skill_guide: free_question" in debug or "free_question" in debug


def test_technical_business_qa_views_switch_same_answer() -> None:
    answer = _rich_answer()
    run = _run("order_service.py 里 create_order 为什么要检查 coupon？")

    tech = render_answer_card(run, answer, audience=AnswerAudience.TECHNICAL)
    business = render_answer_card(run, answer, audience=AnswerAudience.BUSINESS)
    qa = render_answer_card(run, answer, audience=AnswerAudience.QA)
    evidence = render_answer_card(run, answer, audience=AnswerAudience.EVIDENCE)

    tech_text = _joined(tech)
    biz_text = _joined(business)
    qa_text = _joined(qa)
    evid_text = _joined(evidence)

    assert "技术视图" in tech_text
    assert "相关文件" in tech_text or "src/order_service.py" in tech_text
    assert "排查与验证" in tech_text or "验证建议" in tech_text
    assert "不执行 Apply" in tech_text

    assert "业务/产品视图" in biz_text
    assert "业务" in biz_text
    assert "工程当前进度" in biz_text or "需要业务配合" in biz_text

    assert "测试视图" in qa_text
    assert "回归" in qa_text or "验证" in qa_text

    assert "证据视图" in evid_text
    assert "**本次参考来源**" in evid_text
    assert "src/order_service.py" in evid_text or "architecture.md" in evid_text

    # Same underlying facts — no invention of unrelated services.
    for text in (tech_text, biz_text, qa_text):
        assert "下单前校验 coupon" in text or "coupon" in text.casefold()
        assert "BillingGateway" not in text


def test_role_buttons_present_on_team_card() -> None:
    card = render_answer_card(
        _run("介绍一下这个项目"),
        _rich_answer(),
        audience=AnswerAudience.TEAM,
    )
    labels: list[str] = []
    for element in card.get("elements", []):  # type: ignore[union-attr]
        if not isinstance(element, dict) or element.get("tag") != "action":
            continue
        for action in element.get("actions") or []:
            if isinstance(action, dict):
                labels.append(action["text"]["content"])
    assert "给技术看的版本" in labels
    assert "给产品/业务看的版本" in labels
    assert "给测试看的版本" in labels
    assert "查看证据" in labels


def test_skillguide_provider_tool_ids_not_on_team_first_screen() -> None:
    answer = _rich_answer()
    card = render_answer_card(
        _run("这个入口影响什么？"),
        answer,
        audience=AnswerAudience.TEAM,
        show_debug_audit=False,
    )
    first = _first_screen(card, n=8)
    joined = _joined(card)
    assert "SkillGuide" not in first
    assert "free_question" not in first
    assert "stub_planner" not in first
    assert "search_context" not in first
    assert "read_project_file" not in first
    # Without debug audit, team cards must not leak trace_id at all.
    assert "trace_id" not in joined
    assert "**回答**" in joined
    assert "**调试信息**" not in joined
