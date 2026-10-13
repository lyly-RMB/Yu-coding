"""Feishu AnswerView experience: user-centered cards, not Skill debug reports."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

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
from project_lens.integrations.feishu.audit_summary import FeishuAuditSummary
from project_lens.integrations.feishu.views import (
    AnswerView,
    DEGRADED_ANSWER_TITLE,
    confidence_status_label,
    human_evidence_label,
    is_degraded_answer,
    is_evidence_insufficient,
    select_answer_view,
)
from project_lens.integrations.feishu.cards import format_answer_as_text, format_failure_as_text, render_answer_card
from project_lens.main import create_app


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


def _evidence(*, path: str = "docs/architecture.md") -> Evidence:
    return Evidence(
        type=EvidenceType.DOCUMENT,
        project=_project(),
        source=SourceRef(system="local", source_id=path),
        content="architecture content",
        observed_at=datetime.now(timezone.utc),
        access_scope="project:payment:read",
        content_hash="1234567890abcdefaa",
        metadata={"path": path},
    )


def _answer(
    *,
    skill: str,
    confidence: float = 0.8,
    evidence: tuple[Evidence, ...] = (),
    claims: tuple[Claim, ...] = (),
    unknowns: tuple[str, ...] = (),
) -> ProjectAnswer:
    return ProjectAnswer(
        project=_project(),
        status="identified",
        skill=skill,
        confidence=confidence,
        business_summary="一句话业务结论",
        technical_summary="技术细节",
        claims=claims,
        evidence=evidence,
        unknowns=unknowns,
    )


def test_select_answer_view_by_question_and_skill(monkeypatch) -> None:
    from project_lens.config import settings

    monkeypatch.setattr(settings, "project_skill_routing_enabled", True)
    evidence = (_evidence(),)
    claim = Claim(
        text="项目定位：支付演示服务",
        type=ClaimType.FACT,
        evidence_ids=(evidence[0].id,),
        grade=EvidenceGrade.B,
    )
    assert (
        select_answer_view(
            _run("介绍一下这个项目"),
            _answer(skill="project_knowledge", evidence=evidence, claims=(claim,)),
        )
        == AnswerView.PROJECT_OVERVIEW
    )
    assert (
        select_answer_view(
            _run("请解释这个项目的架构、服务、入口、依赖和风险，形成项目地图。"),
            _answer(skill="architecture", evidence=evidence, claims=(claim,)),
        )
        == AnswerView.PROJECT_MAP
    )
    assert (
        select_answer_view(
            _run("这个版本上线后影响了什么"),
            _answer(skill="version_change", evidence=evidence, claims=(claim,)),
        )
        == AnswerView.CHANGE_IMPACT
    )
    assert (
        select_answer_view(
            _run("AttributeError on coupon"),
            _answer(skill="incident_diagnosis", evidence=evidence, claims=(claim,)),
        )
        == AnswerView.INCIDENT_COLLABORATION
    )
    assert (
        select_answer_view(
            _run("知识库缺什么"),
            _answer(skill="project_knowledge", evidence=evidence, claims=(claim,)),
        )
        == AnswerView.KNOWLEDGE_GAP
    )


def test_intro_card_has_overview_title_without_skill_on_first_screen(monkeypatch) -> None:
    from project_lens.config import settings

    monkeypatch.setattr(settings, "project_skill_routing_enabled", True)
    evidence = (_evidence(path="README.md"),)
    answer = _answer(
        skill="project_knowledge",
        evidence=evidence,
        claims=(
            Claim(
                text="项目定位：支付订单演示",
                type=ClaimType.FACT,
                evidence_ids=(evidence[0].id,),
                grade=EvidenceGrade.B,
            ),
        ),
    )
    card = render_answer_card(_run("介绍一下这个项目"), answer)
    assert card["header"]["title"]["content"] == "ProjectLens 项目概览"
    contents = [item.get("content", "") for item in card["elements"]]
    assert contents
    assert not contents[0].startswith("**当前 Skill**")
    assert all(not item.startswith("**当前 Skill**") for item in contents)
    joined = "\n".join(contents)
    assert "**你问的是**" in joined
    assert "**回答**" in joined
    assert "**来源摘要**" in joined
    assert "**调试信息**" in joined
    assert "内部路由：project_knowledge" in joined
    # Skill only in debug zone, after main content.
    debug_idx = joined.index("**调试信息**")
    ask_idx = joined.index("**你问的是**")
    assert ask_idx < debug_idx


def test_card_prefers_complete_hermes_answer_text() -> None:
    answer = _answer(skill="project_knowledge").model_copy(
        update={"answer_text": "产品：范围已确认\n\n研发：接口待补测\n\n测试：回归进行中"}
    )
    card = render_answer_card(_run("总结当前进展"), answer)
    joined = "\n".join(str(item.get("content") or "") for item in card["elements"])
    assert "产品：范围已确认" in joined
    assert "研发：接口待补测" in joined
    assert "测试：回归进行中" in joined
    assert joined.count("产品：范围已确认") == 1
    assert "### 技术视图" not in joined


def test_question_topic_does_not_implicitly_switch_role_view() -> None:
    answer = _answer(skill="project_knowledge").model_copy(
        update={"answer_text": "架构包含 API、Evidence 和 Hermes 三层。"}
    )
    card = render_answer_card(_run("介绍一下系统技术架构"), answer)
    joined = "\n".join(str(item.get("content") or "") for item in card["elements"])
    assert "**回答**\n架构包含 API、Evidence 和 Hermes 三层。" in joined
    assert "### 技术视图" not in joined
    assert joined.count("架构包含 API、Evidence 和 Hermes 三层。") == 1


def test_insufficient_evidence_fallback_is_actionable() -> None:
    answer = _answer(
        skill="project_knowledge",
        confidence=0.0,
        evidence=(),
        claims=(),
        unknowns=("缺少架构文档", "缺少负责人信息"),
    )
    # Force empty summaries so the fallback leads (otherwise summaries still render).
    answer = answer.model_copy(
        update={"business_summary": "", "technical_summary": "", "conclusion": ""}
    )
    assert is_evidence_insufficient(answer) is True
    assert is_degraded_answer(answer) is True
    card = render_answer_card(_run("介绍一下这个项目"), answer)
    assert card["header"]["title"]["content"] == DEGRADED_ANSWER_TITLE
    joined = "\n".join(item.get("content", "") for item in card["elements"])
    assert "【降级标注】" in joined
    assert "HERMES_INSUFFICIENT_EVIDENCE" in joined
    assert "hermes.evidence" in joined
    assert "我查了什么" in joined or "我已经知道" in joined
    assert "还缺" in joined
    assert "建议" in joined
    assert "置信度 0%" not in joined
    assert confidence_status_label(0.0, has_evidence=False) == "当前没有可用资料"


def test_uncited_fact_is_normal_answer_with_source_line() -> None:
    """Missing citations must not degrade — card states「引用资料：没有」."""

    answer = _answer(
        skill="project_investigation",
        confidence=0.7,
        evidence=(),
        claims=(
            Claim(
                text="默认滑动窗口约 12 轮、token 预算约 6000。",
                type=ClaimType.FACT,
                evidence_ids=(),
                grade=EvidenceGrade.C,
            ),
        ),
        unknowns=(),
    )
    answer = answer.model_copy(
        update={
            "business_summary": "默认滑动窗口约 12 轮、token 预算约 6000。",
            "conclusion": "默认滑动窗口约 12 轮、token 预算约 6000。",
        }
    )
    assert is_degraded_answer(answer) is False
    card = render_answer_card(_run("滑动窗口多大"), answer)
    assert card["header"]["title"]["content"] != DEGRADED_ANSWER_TITLE
    joined = "\n".join(str(item.get("content", "")) for item in card["elements"])
    assert "【降级标注】" not in joined
    assert "引用资料：没有" in joined
    text = format_answer_as_text(_run("滑动窗口多大"), answer)
    assert DEGRADED_ANSWER_TITLE not in text
    assert "引用资料：没有" in text


def test_cited_facts_keep_normal_title_despite_dimension_unknowns() -> None:
    """Partial gaps (owner/changelog) must not mark a citation-checked answer degraded."""

    ev = _evidence()
    answer = _answer(
        skill="project_investigation",
        confidence=0.7,
        evidence=(ev,),
        claims=(
            Claim(
                text="项目定位：订单/支付链路",
                type=ClaimType.FACT,
                evidence_ids=(ev.id,),
                grade=EvidenceGrade.B,
            ),
        ),
        unknowns=(
            "负责人：fact_type=owner 返回 state: unknown",
            "无发布 / 变更类授权证据",
        ),
    )
    assert is_degraded_answer(answer) is False
    card = render_answer_card(_run("介绍一下这个项目"), answer)
    assert card["header"]["title"]["content"] != DEGRADED_ANSWER_TITLE
    joined = "\n".join(item.get("content", "") for item in card["elements"])
    assert "【降级标注】" not in joined


def test_format_failure_text_marks_non_hermes() -> None:
    run = _run("介绍项目")
    run = run.model_copy(update={"error": "Hermes agent loop failed: boom"})
    text = format_failure_as_text(run)
    assert text.startswith("【错误】")
    assert "HERMES_AGENT_LOOP_FAILED" in text
    assert "hermes.agent_loop" in text
    assert "boom" in text
    assert str(run.id) in text


def test_format_failure_text_marks_model_gateway() -> None:
    run = _run("介绍项目")
    run = run.model_copy(
        update={"error": "[HERMES_MODEL_FAILED@hermes.model] 模型调用失败：HTTP 502"}
    )
    text = format_failure_as_text(run)
    assert "【错误】模型调用失败" in text
    assert "HERMES_MODEL_FAILED" in text
    assert "hermes.model" in text
    assert "HTTP 502" in text


def test_audit_debug_zone_omits_secrets_and_is_not_first_screen(monkeypatch) -> None:
    from project_lens.config import settings

    monkeypatch.setattr(settings, "project_skill_routing_enabled", True)
    evidence = (_evidence(),)
    answer = _answer(
        skill="architecture",
        evidence=evidence,
        claims=(
            Claim(
                text="核心服务：order-service",
                type=ClaimType.FACT,
                evidence_ids=(evidence[0].id,),
                grade=EvidenceGrade.B,
            ),
        ),
    )
    summary = FeishuAuditSummary(
        skill="architecture",
        provider="stub",
        model_name="stub-v1",
        evidence_count="1",
        trace_id=str(uuid4()),
        allow_apply="False",
    )
    card = render_answer_card(
        _run("请解释这个项目的架构、服务、入口、依赖和风险，形成项目地图。"),
        answer,
        audit_summary=summary,
    )
    assert card["header"]["title"]["content"] == "ProjectLens 项目地图"
    contents = [item.get("content", "") for item in card["elements"]]
    assert contents[0].startswith("**你问的是**")
    joined = "\n".join(contents)
    assert "**本次回答依据 / Audit 摘要**" not in joined
    assert "**调试信息**" in joined
    assert "api_key" not in joined.lower()
    assert "sk-" not in joined.lower()
    assert "**回答**" in joined
    assert "**来源摘要**" in joined


def test_human_evidence_label_prefers_path_title() -> None:
    item = _evidence(path="docs/owners.md")
    assert human_evidence_label(item) == "docs/owners.md"


def test_feishu_intro_live_path_uses_overview_view(monkeypatch) -> None:
    from project_lens.config import settings
    from tests.test_feishu_integration import _configure_verifier, _message_payload

    monkeypatch.setattr(settings, "project_skill_routing_enabled", True)
    app = create_app()
    _configure_verifier(app)
    client = TestClient(app)
    response = client.post(
        "/api/v1/feishu/events",
        json=_message_payload(event_id="answer-view-intro", text="介绍一下这个项目"),
    )
    assert response.status_code == 200
    card = app.state.feishu_messenger.messages[-1].content
    title = card["header"]["title"]["content"]
    # S03: project @ opens context preview first; Hermes answer comes after「直接回答」.
    if title == "ProjectLens 上下文预览":
        assert "待发上下文" in str(card) or "直接回答" in str(card)
        return
    assert title in {DEGRADED_ANSWER_TITLE, "ProjectLens 项目概览"}
    card_text = "\n".join(
        element.get("content", "")
        for element in card.get("elements", [])
        if isinstance(element, dict)
    )
    assert "**当前 Skill**" not in card_text
    if title == DEGRADED_ANSWER_TITLE:
        assert "【降级标注】" in card_text
        assert (
            "Evidence is insufficient." in card_text
            or "当前没有" in card_text
            or "当前资料" in card_text
            or "我查了什么" in card_text
        )
        assert "**一句话结论**" not in card_text
        assert "**来源摘要**" not in card_text
    else:
        assert "**一句话结论**" in card_text or "来源摘要" in card_text or "引用资料" in card_text
